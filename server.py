import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# --- Логика Telegram Бота ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Привет! Отправь мне ИНН компании для проверки рисков.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    await update.message.reply_text(f"🔍 Анализирую запрос по ИНН: {query}...\n(Сервер работает в облаке 24/7!)")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запускаем Telegram-бота при старте FastAPI
    bot_app = None
    if TELEGRAM_BOT_TOKEN:
        bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        bot_app.add_handler(CommandHandler("start", start_command))
        bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling()
        print("🤖 Telegram бот успешно запущен в фоновом режиме!")

    yield  # В этой точке работает FastAPI

    # Остановка бота при завершении работы сервера
    if bot_app:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()

# --- Инициализация FastAPI ---
app = FastAPI(title="Company Risk API", lifespan=lifespan)

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Сервис проверки контрагентов работает!"}

@app.get("/api/company/risks")
def check_company_risks(query: str = Query(..., description="ИНН или название компании")):
    # Здесь твоя существующая логика проверки через DaData
    return {
        "query": query,
        "company_name": "ООО ТЕСТ",
        "inn": query,
        "risk_label": "Низкий риск",
        "critical_risks": [],
        "warnings": []
    }