import os
import time
import html
import json
import asyncio
from typing import Dict, List
from datetime import datetime
from contextlib import asynccontextmanager

import httpx
import gspread
from google.oauth2.service_account import Credentials
from fastapi import FastAPI, HTTPException, Query, Depends, Request
from fastapi.security import APIKeyHeader
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from openai import AsyncOpenAI

load_dotenv()

# --- Конфигурация и Секреты ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API_URL = os.getenv("API_URL", "https://my-check-bot.onrender.com").rstrip("/")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# Настройки GreenAPI (WhatsApp)
GREENAPI_INSTANCE_ID = os.getenv("GREENAPI_INSTANCE_ID", "").strip()
GREENAPI_API_TOKEN = os.getenv("GREENAPI_API_TOKEN", "").strip()

# Настройки Google Таблиц
GOOGLE_SHEETS_CREDENTIALS_FILE = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Лиды СкладПро AI")

# Путь к папке с регламентами
DOCS_FOLDER_PATH = os.getenv("DOCS_FOLDER_PATH", "./knowledge_docs")
os.makedirs(DOCS_FOLDER_PATH, exist_ok=True)

if not INTERNAL_API_KEY:
    if os.getenv("RENDER"):
        raise ValueError("❌ КРИТИЧЕСКАЯ ОШИБКА: Переменная INTERNAL_API_KEY не задана!")
    else:
        INTERNAL_API_KEY = "local_dev_secret_key"

# Защита внутренних API
api_key_header = APIKeyHeader(name="X-Internal-Key", auto_error=False)

async def verify_api_key(api_key: str = Depends(api_key_header)):
    if api_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    return api_key

user_cooldowns: Dict[str, float] = {}
COOLDOWN_SECONDS = 3.0

import json

def add_lead_to_google_sheet(phone: str, request_text: str, status: str = "Новый лид"):
    # 1. Сначала пробуем прочитать из переменной Render
    google_json_str = os.getenv("GOOGLE_CREDENTIALS_JSON")
    
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        
        if google_json_str:
            # Авторизация из строки (для Render)
            creds_dict = json.loads(google_json_str)
            creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        elif os.path.exists(GOOGLE_SHEETS_CREDENTIALS_FILE):
            # Авторизация из файла (для локальной разработки)
            creds = Credentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=scopes)
        else:
            print("⚠️ Ключи Google Таблиц не найдены ни в .env, ни в файле.")
            return False

        client = gspread.authorize(creds)
        sheet = client.open(GOOGLE_SHEET_NAME).sheet1
        
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([now, phone, request_text, status])
        print(f"✅ Лид успешно записан в Google Таблицу: {phone}")
        return True
    except Exception as e:
        print(f"❌ Ошибка записи в Google Таблицу: {e}")
        return False

# --- GreenAPI (Отправка в WhatsApp) ---
async def send_whatsapp_message(chat_id: str, text: str):
    """Отправляет текстовое сообщение пользователю в WhatsApp через GreenAPI."""
    if not GREENAPI_INSTANCE_ID or not GREENAPI_API_TOKEN:
        print("⚠️ GreenAPI не настроен (отсутствует INSTANCE_ID или API_TOKEN).")
        return

    url = f"https://api.green-api.com/waInstance{GREENAPI_INSTANCE_ID}/sendMessage/{GREENAPI_API_TOKEN}"
    payload = {
        "chatId": chat_id,
        "message": text
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(url, json=payload)
            if res.status_code == 200:
                print(f"📤 Сообщение отправлено в WhatsApp [{chat_id}]")
            else:
                print(f"❌ Ошибка отправки WhatsApp: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"❌ Ошибка связи с GreenAPI: {e}")

# --- Поиск по Базе Знаний (RAG) ---
async def search_via_mcp_agent(question: str) -> Dict[str, str]:
    if not OPENAI_API_KEY:
        return {
            "question": question,
            "answer": "Ошибка: OPENAI_API_KEY не задан.",
            "source_document": "Конфигурация"
        }

    docs_context = ""
    sources_found = []

    if os.path.exists(DOCS_FOLDER_PATH):
        for filename in sorted(os.listdir(DOCS_FOLDER_PATH)):
            if filename.endswith((".txt", ".md")):
                file_path = os.path.join(DOCS_FOLDER_PATH, filename)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                        if content:
                            docs_context += f"\n--- ДОКУМЕНТ: {filename} ---\n{content}\n"
                            sources_found.append(filename)
                except Exception as e:
                    print(f"Ошибка чтения {filename}: {e}")

    if not docs_context:
        return {
            "question": question,
            "answer": "В базе знаний нет загруженных регламентов.",
            "source_document": "Пусто"
        }

    try:
        openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        
        system_prompt = (
            "Ты — умный и вежливый бизнес-ассистент сервиса складского учета СкладПро.\n"
            "Твоя цель — консультировать потенциальных и текущих клиентов по услугам, тарифам, "
            "демо-доступу и технической поддержке интеграции с 1С.\n\n"
            "ПРАВИЛА ОТВЕТА:\n"
            "1. Отвечай строго на основе предоставленного ниже контекста базы знаний.\n"
            "2. SKU (Stock Keeping Unit) — это уникальные товарные позиции на складе.\n"
            "3. Если клиент хочет демо-доступ или готов оформить тариф, попроси у него имя и название компании, "
            "после чего подтверди, что передал заявку менеджеру.\n"
            "4. Пиши грамотно, структурировано и коротко.\n\n"
            f"КОНТЕКСТ БАЗЫ ЗНАНИЙ:\n{docs_context}"
        )

        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ],
            temperature=0.2
        )

        answer_text = response.choices[0].message.content
        source_str = ", ".join(sources_found) if sources_found else "База знаний СкладПро"

        return {
            "question": question,
            "answer": answer_text,
            "source_document": source_str
        }

    except Exception as e:
        print(f"Ошибка OpenAI API: {e}")
        return {
            "question": question,
            "answer": f"Произошла ошибка при обращении к нейросети: {e}",
            "source_document": "Ошибка OpenAI"
        }

# --- Telegram Бот Handlers (Интерфейс менеджера/тестирования) ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Здравствуйте! Я виртуальный ассистент СкладПро.</b>\n\n"
        "Задайте ваш вопрос по тарифам, 1С или демо-доступу!",
        parse_mode="HTML"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    current_time = time.time()
    query = update.message.text.strip()

    if current_time - user_cooldowns.get(user_id, 0) < COOLDOWN_SECONDS:
        await update.message.reply_text("⏳ Пожалуйста, подождите пару секунд.")
        return

    user_cooldowns[user_id] = current_time
    headers = {"X-Internal-Key": INTERNAL_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"{API_URL}/api/knowledge/query",
                params={"question": query},
                headers=headers
            )
            if response.status_code == 200:
                data = response.json()
                answer = html.escape(data.get("answer", ""))
                source = html.escape(data.get("source_document", ""))

                reply_text = f"🤖 <b>Ответ:</b>\n\n{answer}\n\n📄 <b>Источник:</b> <code>{source}</code>"
                await update.message.reply_text(reply_text, parse_mode="HTML")
            else:
                await update.message.reply_text("❌ Ошибка обработки запроса.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка связи: {e}")

# --- Lifespan ---
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
        print("🤖 Telegram бот запущен!")

    yield

    if bot_app:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()

# --- FastAPI App ---
app = FastAPI(title="СкладПро AI Support API", lifespan=lifespan)

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Сервис СкладПро AI работает!"}

@app.get("/api/knowledge/query", dependencies=[Depends(verify_api_key)])
async def query_knowledge_base(question: str = Query(..., description="Вопрос по продукту СкладПро")):
    result = await search_via_mcp_agent(question)
    return result

# --- Webhook для GreenAPI (WhatsApp) ---
@app.post("/api/whatsapp/webhook")
async def whatsapp_webhook(request: Request):
    """Принимает входящие сообщения из WhatsApp от GreenAPI."""
    try:
        payload = await request.json()
        type_webhook = payload.get("typeWebhook")

        # Проверяем, что это входящее сообщение от клиента
        if type_webhook == "incomingMessageReceived":
            message_data = payload.get("messageData", {})
            sender_data = payload.get("senderData", {})

            chat_id = sender_data.get("chatId")  # Например: "79991112233@c.us"
            phone = sender_data.get("sender", "").replace("@c.us", "")
            text_message = message_data.get("textMessageData", {}).get("textMessage", "").strip()

            if chat_id and text_message:
                print(f"📩 Входящее сообщение WhatsApp от {phone}: {text_message}")

                # 1. Запрос к AI
                ai_result = await search_via_mcp_agent(text_message)
                answer_text = ai_result.get("answer", "")

                # 2. Отправка ответа в WhatsApp
                await send_whatsapp_message(chat_id, answer_text)

                # 3. Если в запросе или ответе есть ключевые слова лида — фиксируем в Google Таблицу
                keywords = ["демо", "тарифа", "подключить", "купить", "стоимость", "демонстрация", "тест"]
                if any(kw in text_message.lower() for kw in keywords):
                    add_lead_to_google_sheet(
                        phone=phone,
                        request_text=text_message,
                        status="Запрос демо / тарифа"
                    )

    except Exception as e:
        print(f"❌ Ошибка при обработке WhatsApp Webhook: {e}")

    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
