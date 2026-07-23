import os
import time
import html
import asyncio
from typing import Dict, List
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, HTTPException, Query, Depends
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

# Путь к папке с регламентами и документами
DOCS_FOLDER_PATH = os.getenv("DOCS_FOLDER_PATH", "./knowledge_docs")

# Создаем папку сразу при запуске скрипта, если её нет
os.makedirs(DOCS_FOLDER_PATH, exist_ok=True)

if not INTERNAL_API_KEY:
    if os.getenv("RENDER"):
        raise ValueError("❌ КРИТИЧЕСКАЯ ОШИБКА: Переменная INTERNAL_API_KEY не задана в Render Environment Variables!")
    else:
        INTERNAL_API_KEY = "local_dev_secret_key"

# Настройка защиты API
api_key_header = APIKeyHeader(name="X-Internal-Key", auto_error=False)

async def verify_api_key(api_key: str = Depends(api_key_header)):
    if api_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=403, detail="Доступ запрещен: Неверный или отсутствующий API-ключ")
    return api_key

user_cooldowns: Dict[int, float] = {}
COOLDOWN_SECONDS = 3.0

def is_valid_inn(inn: str) -> bool:
    return inn.isdigit() and len(inn) in (10, 12)

# --- Надежная Логика Поиска по Базе Знаний (RAG) ---
async def search_via_mcp_agent(question: str) -> Dict[str, str]:
    """
    Сканирует файлы из knowledge_docs и генерирует ответ на основе их содержимого.
    """
    if not OPENAI_API_KEY:
        return {
            "question": question,
            "answer": "Ошибка: Переменная OPENAI_API_KEY не задана в окружении (.env).",
            "source_document": "Конфигурация сервера"
        }

    # 1. Читаем файлы из папки knowledge_docs
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
                    print(f"Ошибка чтения файла {filename}: {e}")

    if not docs_context:
        return {
            "question": question,
            "answer": "В базе знаний пока нет загруженных регламентов (папка knowledge_docs пуста или файлы не содержат текст).",
            "source_document": "Документы не найдены"
        }

    # 2. Запрос к OpenAI
    try:
        openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        
        system_prompt = (
            "Ты — корпоративный ассистент компании. Отвечай на вопрос сотрудника, "
            "используя ТОЛЬКО предоставленный ниже контекст документов.\n"
            "Если в контексте нет ответа на вопрос, вежливо скажи, что в регламентах этого нет.\n"
            "Форматируй ответ четко и структурировано.\n\n"
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
        source_str = ", ".join(sources_found) if sources_found else "База знаний компании"

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
            "source_document": " Ошибка OpenAI API"
        }

# --- Telegram Бот Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Привет! Я твой виртуальный ассистент.</b>\n\n"
        "1️⃣ <b>Проверка контрагентов:</b> Отправь мне <b>ИНН</b> (10 или 12 цифр).\n"
        "2️⃣ <b>База знаний:</b> Задай любой вопрос по регламентам и документам компании.",
        parse_mode="HTML"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_time = time.time()
    query = update.message.text.strip()

    # Защита от спама
    last_request_time = user_cooldowns.get(user_id, 0)
    if current_time - last_request_time < COOLDOWN_SECONDS:
        remaining = int(COOLDOWN_SECONDS - (current_time - last_request_time)) + 1
        await update.message.reply_text(f"⏳ Пожалуйста, подождите {remaining} сек.")
        return

    user_cooldowns[user_id] = current_time
    headers = {"X-Internal-Key": INTERNAL_API_KEY}

    # ==========================================
    # ВЕТКА 1: Проверка ИНН
    # ==========================================
    if is_valid_inn(query):
        await update.message.reply_text(f"🔍 Анализирую запрос по ИНН: <code>{html.escape(query)}</code>...", parse_mode="HTML")
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.get(
                    f"{API_URL}/api/company/risks",
                    params={"query": query},
                    headers=headers
                )
                if response.status_code == 200:
                    data = response.json()
                    reply_text = (
                        f"🏢 <b>Результат проверки</b>\n\n"
                        f"📌 <b>Компания:</b> {html.escape(str(data.get('company_name', 'Н/Д')))}\n"
                        f"🔢 <b>ИНН:</b> <code>{html.escape(str(data.get('inn')))}</code>\n"
                        f"📊 <b>Уровень риска:</b> {html.escape(str(data.get('risk_label')))}\n"
                    )
                    await update.message.reply_text(reply_text, parse_mode="HTML")
                else:
                    await update.message.reply_text(f"❌ Ошибка API (код {response.status_code}).")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка связи с сервером: {e}")
        
        return

    # ==========================================
    # ВЕТКА 2: База Знаний
    # ==========================================
    await update.message.reply_text(f"📚 Ищу ответ в регламентах: <i>\"{html.escape(query)}\"</i>...", parse_mode="HTML")
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"{API_URL}/api/knowledge/query",
                params={"question": query},
                headers=headers
            )
            if response.status_code == 200:
                data = response.json()
                answer = html.escape(data.get("answer", "Не удалось найти ответ."))
                source = html.escape(data.get("source_document", "Источник не указан"))

                reply_text = (
                    f"🤖 <b>Ответ из базы знаний:</b>\n\n"
                    f"{answer}\n\n"
                    f"📄 <b>Источник:</b> <code>{source}</code>"
                )
                await update.message.reply_text(reply_text, parse_mode="HTML")
            else:
                await update.message.reply_text(f"❌ Ошибка API знаний (код {response.status_code}).")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка связи с сервером: {e}")

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
        print("🤖 Telegram бот с поддержкой Базы Знаний успешно запущен!")

    yield

    if bot_app:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()

# --- FastAPI App ---
app = FastAPI(title="Company Risk & Knowledge Base API", lifespan=lifespan)

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Сервис проверки контрагентов и База Знаний работают!"}

@app.get("/api/company/risks", dependencies=[Depends(verify_api_key)])
def check_company_risks(query: str = Query(..., description="ИНН компании (10 или 12 цифр)")):
    if not is_valid_inn(query):
        raise HTTPException(status_code=400, detail="ИНН должен состоять из 10 или 12 цифр")

    return {
        "query": query,
        "company_name": "ООО ТЕСТ",
        "inn": query,
        "risk_label": "🟢 Низкий риск",
        "critical_risks": [],
        "warnings": []
    }

@app.get("/api/knowledge/query", dependencies=[Depends(verify_api_key)])
async def query_knowledge_base(question: str = Query(..., description="Вопрос по регламентам")):
    """Эндпоинт обращения к Базе Знаний для поиска по регламентам"""
    result = await search_via_mcp_agent(question)
    return result

# Точка входа для локального запуска
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
