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

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID", "")
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "searches_config.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "make": "BMW",
            "model": "318",
            "max_price": "20000",
            "max_km": "120000",
            "zip_code": "",
            "radius": "",
        }


def save_config(config):
    temporary = CONFIG_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(CONFIG_FILE)


def allowed(update):
    # Si CHAT_ID no está configurado, no se restringe (cualquiera puede usar
    # el bot). Si está configurado, solo responde en ese chat/grupo concreto.
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
            [InlineKeyboardButton("🔍 Buscar ahora", callback_data="search")],
        ]
    )


async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text("Panel de búsqueda de vehículos", reply_markup=menu(load_config()))


async def stream_subprocess_output(stream, tag):
    """
    Lee la salida de main.py línea a línea y la imprime al instante (con
    flush=True), en vez de esperar a que el proceso termine del todo. Así,
    si el scraping se cuelga, se puede ver en el Live Tail de Render en qué
    paso exacto se quedó parado, en lugar de descubrirlo solo tras el
    timeout sin ninguna pista.
    """
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

    if action != "search":
        context.user_data["field"] = action
        labels = {
            "make": "marca",
            "model": "modelo",
            "max_price": "precio máximo",
            "max_km": "kilómetros máximos",
        }
        await query.message.reply_text(
            f"Escribe el nuevo valor para {labels[action]}. Escribe 'ninguno' para vaciarlo."
        )
        return

    await query.message.reply_text("🔎 Buscando; puede tardar hasta tres minutos…")

    env = os.environ.copy()
    env["RUN_ONCE"] = "1"  # Evita que main.py se quede en su bucle horario.

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
        # Se espera a que las tareas de lectura vacíen lo que ya se había
        # impreso antes de matar el proceso, para no perder esas últimas
        # líneas (que son justo las más útiles para saber dónde se atascó).
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        await query.message.reply_text(
            "⏱️ Tiempo agotado tras 210s. Revisa el Live Tail de Render: ahí verás en qué paso "
            "exacto se quedó parado el rastreo (por ejemplo, en qué página o plataforma)."
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
    """
    Servidor HTTP mínimo, solo para que Render (configurado como "Web
    Service") detecte un puerto abierto y no reinicie el proceso en bucle.
    No hace nada más que responder 200 OK a cualquier petición.
    """
    port = int(os.environ.get("PORT", 10000))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, format, *args):
            pass  # silencia el log de cada ping para no ensuciar la consola

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


def main():
    if not TOKEN:
        sys.exit("Configura TELEGRAM_TOKEN antes de iniciar el bot.")

    Thread(target=health_server, daemon=True).start()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "config", "menu"], start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text))

    log.info("Bot interactivo de Telegram iniciado...")
    app.run_polling()


if __name__ == "__main__":
    main()
