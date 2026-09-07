import asyncio
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID", "")
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "searches_config.json"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"make": "BMW", "model": "318", "max_price": "20000", "max_km": "120000", "zip_code": "", "radius": ""}


def save_config(config):
    temporary = CONFIG_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(CONFIG_FILE)


def allowed(update):
    return not CHAT_ID or str(update.effective_chat.id) == str(CHAT_ID)


def menu(config):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🚗 Marca: {config.get('make') or 'Todas'}", callback_data="make"), InlineKeyboardButton(f"📝 Modelo: {config.get('model') or 'Todos'}", callback_data="model")],
        [InlineKeyboardButton(f"💶 Máx.: {config.get('max_price') or '—'} €", callback_data="max_price"), InlineKeyboardButton(f"🛣️ Máx.: {config.get('max_km') or '—'} km", callback_data="max_km")],
        [InlineKeyboardButton("🔍 Buscar ahora", callback_data="search")],
    ])


async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text("Panel de búsqueda de vehículos", reply_markup=menu(load_config()))


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    query = update.callback_query
    await query.answer()
    action = query.data

    if action != "search":
        context.user_data["field"] = action
        labels = {"make": "marca", "model": "modelo", "max_price": "precio máximo", "max_km": "kilómetros máximos"}
        await query.message.reply_text(f"Escribe el nuevo valor para {labels[action]}. Escribe 'ninguno' para vaciarlo.")
        return

    await query.message.reply_text("🔎 Buscando; puede tardar hasta tres minutos…")
    env = os.environ.copy()
    env["RUN_ONCE"] = "1"  # Evita que main.py se quede en su bucle horario.
    process = await asyncio.create_subprocess_exec(sys.executable, "main.py", cwd=str(BASE_DIR), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=210)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        await query.message.reply_text("⏱️ Tiempo agotado. Revisa el log de mobile.de/Playwright.")
        return

    output = (stderr if process.returncode else stdout).decode(errors="replace").strip()
    status = "✅ Búsqueda terminada" if process.returncode == 0 else "❌ Error al buscar"
    await query.message.reply_text(f"{status}\n<pre>{output[-300:] or 'Sin salida.'}</pre>", parse_mode="HTML")


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
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
