import os
import asyncio
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, HTTPException, Query
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Если API_URL не задан в переменной окружения, используем адрес с Render
# Замени 'my-check-bot.onrender.com' на свой точный домен Render, если он отличается
API_URL = os.getenv("API_URL", "https://my-check-bot.onrender.com").rstrip("/")

# --- Логика Telegram Бота ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Привет! Отправь мне ИНН компании для проверки рисков.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    
    # 1. Отправляем временный статус
    await update.message.reply_text(f"🔍 Анализирую запрос по ИНН: {query}...\n(Сервер работает в облаке 24/7!)")

    # 2. Делаем запрос к нашему FastAPI эндпоинту
    try:
        # Увеличиваем timeout до 60 сек на случай, если бесплатный сервер Render "просыпается"
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(f"{API_URL}/api/company/risks", params={"query": query})
            
            if response.status_code == 200:
                data = response.json()
                
                # Формируем красивый текст ответа
                reply_text = (
                    f"🏢 **Результат проверки**\n\n"
                    f"📌 **Компания:** {data.get('company_name', 'Н/Д')}\n"
                    f"🔢 **ИНН:** {data.get('inn')}\n"
                    f"📊 **Уровень риска:** {data.get('risk_label')}\n"
                )
                
                if data.get("critical_risks"):
                    reply_text += f"\n🚨 **Критические риски:** {', '.join(data['critical_risks'])}"
                if data.get("warnings"):
                    reply_text += f"\n⚠️ **Предупреждения:** {', '.join(data['warnings'])}"
                    
                await update.message.reply_text(reply_text, parse_mode="Markdown")
            else:
                await update.message.reply_text(f"❌ Ошибка API (код {response.status_code}). Попробуйте позже.")

    except httpx.TimeoutException:
        await update.message.reply_text("⏳ Сервер долго не отвечал (просыпался). Попробуйте отправить ИНН еще раз!")
    except Exception as e:
        await update.message.reply_text(f"❌ Произошла ошибка при связи с сервером: {e}")

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
    # Здесь твоя логика проверки (например, через DaData)
    return {
        "query": query,
        "company_name": "ООО ТЕСТ",
        "inn": query,
        "risk_label": "🟢 Низкий риск",
        "critical_risks": [],
        "warnings": []
    }
