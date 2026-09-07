import os
import json
import logging
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Configuración de logs
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CONFIG_FILE = "searches_config.json"


# --- PARCHE RENDER (Opción B) --------------------------------------------
# Render, al estar este servicio configurado como "Web Service", espera que
# el proceso abra el puerto indicado en la variable de entorno PORT y
# responda a peticiones HTTP. Como este bot solo hace long-polling contra
# la API de Telegram (no es una app web), Render no detectaba ningún puerto
# abierto y terminaba matando/reiniciando el proceso en bucle.
#
# Este mini servidor HTTP no hace nada funcional: solo responde "OK" a
# cualquier petición GET para que Render considere el servicio "vivo" y
# deje de reiniciarlo. Corre en un hilo aparte (daemon) para no bloquear
# el polling del bot, que sigue siendo el proceso principal.
def run_health_server():
    port = int(os.environ.get("PORT", 10000))

    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, format, *args):
            # Silencia el log de cada ping de Render para no ensuciar la consola
            pass

    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logging.info("Servidor de health-check escuchando en el puerto %s", port)
    server.serve_forever()
# ---------------------------------------------------------------------------


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"make": "BMW", "model": "318", "max_price": "20000", "max_km": "120000"}

def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

def build_menu(config):
    keyboard = [
        [
            InlineKeyboardButton(f"🚗 Marca: {config.get('make', 'Todas')}", callback_data="set_make"),
            InlineKeyboardButton(f"💶 Máx Price: {config.get('max_price', 'N/A')}€", callback_data="set_price"),
        ],
        [
            InlineKeyboardButton(f"🛣️ Máx Km: {config.get('max_km', 'N/A')}km", callback_data="set_km"),
            InlineKeyboardButton(f"📝 Modelo: {config.get('model', 'Todos')}", callback_data="set_model"),
        ],
        [
            InlineKeyboardButton("🔍 ¡Ejecutar Búsqueda Ahora!", callback_data="run_search"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    reply_markup = build_menu(config)
    await update.message.reply_text(
        "⚙️ **Panel de Control de Búsqueda de Vehículos REBU**\n\nUsa los botones para modificar los filtros:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    config = load_config()

    if data == "run_search":
        await query.edit_message_text("🔎 Iniciando rastreo en AutoScout24, mobile.de y Kleinanzeigen... Por favor espera.")
        try:
            # Lanza main.py como proceso independiente en segundo plano
            process = await asyncio.create_subprocess_exec(
                "python", "main.py",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Establece un tiempo máximo de espera de 180 segundos (3 minutos)
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180.0)

            if process.returncode == 0:
                out = stdout.decode().strip() or "Búsqueda finalizada sin errores."
                await query.message.reply_text(f"✅ **Rastreo completado:**\n```\n{out[-300:]}\n```", parse_mode="Markdown")
            else:
                err = stderr.decode().strip() or stdout.decode().strip() or "Error en script."
                await query.message.reply_text(f"❌ **Fallo al ejecutar main.py:**\n```\n{err[-400:]}\n```", parse_mode="Markdown")

        except asyncio.TimeoutError:
            try:
                process.kill()
            except Exception:
                pass
            await query.message.reply_text("⏱️ **Tiempo agotado:** El rastreador tardó más de 3 minutos y fue detenido. Revisa la configuración `headless` de Playwright en `main.py`.")
        except Exception as e:
            await query.message.reply_text(f"❌ Error al lanzar el proceso: {e}")
        
        # Volver a mostrar el menú interactivo tras finalizar
        reply_markup = build_menu(config)
        await query.message.reply_text("⚙️ **Panel de Control:**", reply_markup=reply_markup, parse_mode="Markdown")

    elif data == "set_price":
        context.user_data['awaiting'] = 'max_price'
        await query.message.reply_text("💶 Responde a este mensaje con el nuevo **Precio Máximo** (ejemplo: `25000`):")

    elif data == "set_km":
        context.user_data['awaiting'] = 'max_km'
        await query.message.reply_text("🛣️ Responde a este mensaje con los nuevos **Kilómetros Máximos** (ejemplo: `120000`):")

    elif data == "set_make":
        context.user_data['awaiting'] = 'make'
        await query.message.reply_text("🚗 Responde a este mensaje con la **Marca** deseada (ejemplo: `BMW`, `Audi`, `Mercedes-Benz`):")

    elif data == "set_model":
        context.user_data['awaiting'] = 'model'
        await query.message.reply_text("📝 Responde a este mensaje con el **Modelo** deseado (ejemplo: `318`, `A4`, o escribe `ninguno` para borrar):")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    awaiting = context.user_data.get('awaiting')
    if not awaiting:
        return

    val = update.message.text.strip()
    config = load_config()

    if awaiting == 'model' and val.lower() == 'ninguno':
        val = ""

    config[awaiting] = val
    save_config(config)
    context.user_data['awaiting'] = None

    reply_markup = build_menu(config)
    await update.message.reply_text(f"✅ Campo **{awaiting}** actualizado a: `{val}`\n\nPanel actualizado:", reply_markup=reply_markup, parse_mode="Markdown")

def main_bot():
    if not TELEGRAM_TOKEN:
        print("ERROR: TELEGRAM_TOKEN no configurado.")
        return

    # Arranca el servidor de health-check en un hilo daemon aparte, ANTES de
    # iniciar el polling. Al ser daemon=True, este hilo no impide que el
    # proceso termine si el hilo principal termina.
    threading.Thread(target=run_health_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler(["start", "config", "menu"], start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("Bot interactivo de Telegram iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main_bot()
