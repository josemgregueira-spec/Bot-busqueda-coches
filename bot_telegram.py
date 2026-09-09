import asyncio
import html
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from scraper import run_pipeline

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID", "")
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "searches_config.json"

# Cada cuánto rastrea en modo vigilancia (segundos). 900 = 15 minutos.
# Puedes cambiarlo en Render con la variable CHECK_INTERVAL_SECONDS.
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "900"))
VIGILANCIA_JOB = "vigilancia"
RUNNING = {"vigilancia": False}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "make": "bmw",
            "model": "",
            "max_price": "20000",
            "max_km": "150000",
            "zip_code": "",
            "radius": "",
            "vigilancia": False,
        }


def save_config(config):
    temporary = CONFIG_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(CONFIG_FILE)


def allowed(update):
    return not CHAT_ID or str(update.effective_chat.id) == str(CHAT_ID)


def menu(config):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"🚗 Marca: {config.get('make') or 'Todas'}", callback_data="make"),
                InlineKeyboardButton(f"📝 Modelo: {config.get('model') or 'Todos'}", callback_data="model"),
            ],
            [
                InlineKeyboardButton(f"💶 Máx.: {config.get('max_price') or '—'} €", callback_data="max_price"),
                InlineKeyboardButton(f"🛣️ Máx.: {config.get('max_km') or '—'} km", callback_data="max_km"),
            ],
            [
                InlineKeyboardButton(
                    f"{'🔴' if config.get('vigilancia') else '⚪'} Vigilancia: {'ON' if config.get('vigilancia') else 'OFF'}",
                    callback_data="vigilancia",
                )
            ],
            [InlineKeyboardButton("🔍 Buscar ahora", callback_data="search")],
        ]
    )


async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text("Panel de búsqueda de vehículos", reply_markup=menu(load_config()))


# ---------------------------------------------------------------------------
# Vigilancia automática: rastrea cada CHECK_INTERVAL segundos y avisa solo
# de los coches nuevos que encajen en los filtros.
# ---------------------------------------------------------------------------

async def vigilancia_job(_context: ContextTypes.DEFAULT_TYPE):
    if RUNNING["vigilancia"]:
        log.info("Vigilancia: el ciclo anterior sigue en curso, se omite este paso.")
        return
    RUNNING["vigilancia"] = True
    log.info("Vigilancia: rastreo automático en curso...")
    try:
        await asyncio.to_thread(run_pipeline)
        log.info("Vigilancia: ciclo completado.")
    except Exception:
        log.exception("Error en el rastreo automático")
    finally:
        RUNNING["vigilancia"] = False


def schedule_vigilancia(app):
    if app.job_queue is None:
        log.error("JobQueue no disponible: añade [job-queue] a python-telegram-bot en requirements.txt")
        return
    if not app.job_queue.get_jobs_by_name(VIGILANCIA_JOB):
        app.job_queue.run_repeating(vigilancia_job, interval=CHECK_INTERVAL, first=10, name=VIGILANCIA_JOB)
        log.info("Vigilancia programada: cada %s segundos.", CHECK_INTERVAL)


def stop_vigilancia(app):
    if not app.job_queue:
        return
    for job in app.job_queue.get_jobs_by_name(VIGILANCIA_JOB):
        job.schedule_removal()


async def stream_subprocess_output(stream, tag):
    lines = []
    while True:
        raw_line = await stream.readline()
        if not raw_line:
            break
        decoded = raw_line.decode(errors="replace").rstrip()
        print(f"[{tag}] {decoded}", flush=True)
        lines.append(decoded)
    return "\n".join(lines)


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return

    query = update.callback_query
    await query.answer()
    action = query.data

    # --- Botón de vigilancia ON/OFF -------------------------------------
    if action == "vigilancia":
        config = load_config()
        nuevo = not config.get("vigilancia", False)
        config["vigilancia"] = nuevo
        save_config(config)
        if nuevo:
            schedule_vigilancia(context.application)
            await query.message.reply_text(
                f"🔔 Vigilancia ACTIVADA. Rastrearé cada {CHECK_INTERVAL // 60} minutos y te avisaré "
                "solo de los coches nuevos que encajen en tu búsqueda (REBU).",
                reply_markup=menu(config),
            )
        else:
            stop_vigilancia(context.application)
            await query.message.reply_text("🔕 Vigilancia desactivada.", reply_markup=menu(config))
        return

    # --- Botón "Buscar ahora" (una pasada puntual) ----------------------
    if action == "search":
        await query.message.reply_text("🔎 Buscando; puede tardar hasta tres minutos…")

        env = os.environ.copy()
        env["RUN_ONCE"] = "1"

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "main.py",
            cwd=str(BASE_DIR),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout_task = asyncio.create_task(stream_subprocess_output(process.stdout, "main.py"))
        stderr_task = asyncio.create_task(stream_subprocess_output(process.stderr, "main.py ERROR"))

        try:
            await asyncio.wait_for(process.wait(), timeout=210)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            await query.message.reply_text(
                "⏱️ Tiempo agotado tras 210s. Revisa el Live Tail de Render para ver en qué paso se quedó parado."
            )
            return

        stdout_text = await stdout_task
        stderr_text = await stderr_task

        output = (stderr_text if process.returncode else stdout_text).strip()
        status = "✅ Búsqueda terminada" if process.returncode == 0 else "❌ Error al buscar"
        await query.message.reply_text(
            f"{status}\n<pre>{html.escape(output[-300:]) or 'Sin salida.'}</pre>",
            parse_mode="HTML",
        )
        return

    # --- Resto de botones: pedir el nuevo valor --------------------------
    context.user_data["field"] = action
    labels = {
        "make": "marca",
        "model": "modelo (escribe 'ninguno' para buscar TODOS los modelos de la marca)",
        "max_price": "precio máximo",
        "max_km": "kilómetros máximos",
    }
    await query.message.reply_text(f"Escribe el nuevo valor para {labels[action]}.")


async def text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return

    field = context.user_data.pop("field", None)
    if not field:
        return

    value = update.message.text.strip()
    config = load_config()
    config[field] = "" if value.lower() == "ninguno" else value
    save_config(config)
    await update.message.reply_text("✅ Configuración guardada.", reply_markup=menu(config))


def health_server():
    port = int(os.environ.get("PORT", 10000))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, format, *args):
            pass

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


def main():
    if not TOKEN:
        sys.exit("Configura TELEGRAM_TOKEN antes de iniciar el bot.")

    Thread(target=health_server, daemon=True).start()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "config", "menu"], start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text))

    # Si la vigilancia quedó activada, la reactivamos al arrancar
    if load_config().get("vigilancia"):
        schedule_vigilancia(app)

    log.info("Bot iniciado. Vigilancia: %s", load_config().get("vigilancia"))
    app.run_polling()


if __name__ == "__main__":
    main()
