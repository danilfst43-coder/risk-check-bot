import os
import time
import asyncio
from typing import Dict
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, HTTPException, Query, Security, Depends
from fastapi.security import APIKeyHeader
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

# --- Конфигурация и Секреты ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API_URL = os.getenv("API_URL", "https://my-check-bot.onrender.com").rstrip("/")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")

# В облаке ключ обязателен! Для локального теста используем фоллбэк
if not INTERNAL_API_KEY:
    if os.getenv("RENDER"):
        raise ValueError("❌ КРИТИЧЕСКАЯ ОШИБКА: Переменная INTERNAL_API_KEY не задана в Render Environment Variables!")
    else:
        INTERNAL_API_KEY = "local_dev_secret_key"

# Настройка защиты API
api_key_header = APIKeyHeader(name="X-Internal-Key", auto_error=False)

async def verify_api_key(api_key: str = Depends(api_key_header)):
    """Проверка секретного ключа для доступа к FastAPI"""
    if api_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=403, detail="Доступ запрещен: Неверный или отсутствующий API-ключ")
    return api_key

# Хранилище для ограничения частоты запросов (User ID -> Время последнего запроса)
user_cooldowns: Dict[int, float] = {}
COOLDOWN_SECONDS = 3.0  # Лимит: 1 запрос в 3 секунды

# --- Валидация ИНН ---
def is_valid_inn(inn: str) -> bool:
    """Проверяет, состоит ли ИНН из 10 (для ЮЛ) или 12 (для ИП/ФЛ) цифр"""
    return inn.isdigit() and len(inn) in (10, 12)

# --- Логика Telegram Бота ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Привет! Отправь мне ИНН компании (10 или 12 цифр) для проверки рисков.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_time = time.time()
    query = update.message.text.strip()

    # 1. Защита от спама (Rate Limiting)
    last_request_time = user_cooldowns.get(user_id, 0)
    if current_time - last_request_time < COOLDOWN_SECONDS:
        remaining = int(COOLDOWN_SECONDS - (current_time - last_request_time)) + 1
        await update.message.reply_text(f"⏳ Пожалуйста, подождите {remaining} сек. перед следующим запросом.")
        return

    # 2. Валидация ИНН на стороне бота (экономит вызовы к серверу)
    if not is_valid_inn(query):
        await update.message.reply_text(
            "⚠️ **Некорректный ИНН!**\n\n"
            "ИНН должен состоять строго из **10 цифр** (для юридических лиц) "
            "или **12 цифр** (для ИП и физлиц). Перепроверьте и отправьте снова.",
            parse_mode="Markdown"
        )
        return

    # Обновляем время последнего успешного запроса пользователя
    user_cooldowns[user_id] = current_time

    # 3. Уведомление пользователя
    await update.message.reply_text(f"🔍 Анализирую запрос по ИНН: `{query}`...\n(Сервер работает в облаке 24/7!)", parse_mode="Markdown")

    # 4. Запрос к внутреннему FastAPI с передачей секретного ключа
    try:
        headers = {"X-Internal-Key": INTERNAL_API_KEY}
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"{API_URL}/api/company/risks", 
                params={"query": query},
                headers=headers
            )
            
            if response.status_code == 200:
                data = response.json()
                reply_text = (
                    f"🏢 **Результат проверки**\n\n"
                    f"📌 **Компания:** {data.get('company_name', 'Н/Д')}\n"
                    f"🔢 **ИНН:** `{data.get('inn')}`\n"
                    f"📊 **Уровень риска:** {data.get('risk_label')}\n"
                )
                if data.get("critical_risks"):
                    reply_text += f"\n🚨 **Критические риски:** {', '.join(data['critical_risks'])}"
                if data.get("warnings"):
                    reply_text += f"\n⚠️ **Предупреждения:** {', '.join(data['warnings'])}"
                    
                await update.message.reply_text(reply_text, parse_mode="Markdown")
            elif response.status_code == 400:
                await update.message.reply_text("⚠️ Ошибка: Передан неверный формат ИНН.")
            elif response.status_code == 403:
                await update.message.reply_text("❌ Ошибка доступа: Сервер отклонил ключ авторизации.")
            else:
                await update.message.reply_text(f"❌ Ошибка API (код {response.status_code}).")

    except httpx.TimeoutException:
        await update.message.reply_text("⏳ Сервер долго не отвечал (просыпался). Попробуйте отправить запрос еще раз!")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при связи с сервером: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    bot_app = None
    if TELEGRAM_BOT_TOKEN:
        bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        bot_app.add_handler(CommandHandler("start", start_command))
        bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling()
        print("🤖 Telegram бот успешно запущен!")

    yield

    if bot_app:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()

# --- Инициализация FastAPI ---
app = FastAPI(title="Company Risk API", lifespan=lifespan)

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Сервис проверки контрагентов работает!"}

# Защищенный эндпоинт проверки рисков
@app.get("/api/company/risks", dependencies=[Depends(verify_api_key)])
def check_company_risks(query: str = Query(..., description="ИНН компании (10 или 12 цифр)")):
    # Валидация ИНН на стороне API
    if not is_valid_inn(query):
        raise HTTPException(status_code=400, detail="ИНН должен состоять из 10 или 12 цифр")

    # Здесь твоя реальная логика запроса к DaData
    return {
        "query": query,
        "company_name": "ООО ТЕСТ",
        "inn": query,
        "risk_label": "🟢 Низкий риск",
        "critical_risks": [],
        "warnings": []
    }
