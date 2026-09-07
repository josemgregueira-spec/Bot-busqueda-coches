import os
import json
import logging
import subprocess
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
    await update.message.reply_text("⚙️ **Panel de Control de Búsqueda de Vehículos REBU**\n\nUsa los botones para modificar los filtros:", reply_markup=reply_markup, parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    config = load_config()

    if data == "run_search":
        await query.edit_message_text("🔎 Iniciando rastreo en AutoScout24, mobile.de y Kleinanzeigen... Por favor espera.")
        try:
            # Ejecuta main.py directamente como un proceso independiente del sistema
            subprocess.run(["python", "main.py"], check=True)
            await query.message.reply_text("✅ Rastreo completado. Si se encontraron vehículos nuevos, habrán sido enviados al grupo.")
        except Exception as e:
            await query.message.reply_text(f"❌ Error durante la búsqueda: {e}")
        
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

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler(["start", "config", "menu"], start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("Bot interactivo de Telegram iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main_bot()
