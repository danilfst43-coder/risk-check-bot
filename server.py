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
            "Ты — умный и вежливый бизнес-ассистент сервиса складского учета СкладПро.\n"
            "Твоя цель — консультировать потенциальных и текущих клиентов по услугам, тарифам, "
            "демо-доступу и технической поддержке интеграции с 1С.\n\n"
            "ПРАВИЛА ОТВЕТА:\n"
            "1. Отвечай строго на основе предоставленного ниже контекста базы знаний.\n"
            "2. SKU (Stock Keeping Unit) в тарифной сетке — это уникальные товарные позиции (номенклатура) "
            "на складе. Умей подробно объяснять, что означает это ограничение в тарифах.\n"
            "3. Помогай с вопросами по расхождению остатков 1С, регламенту настройки, демо-доступу на 14 дней "
            "и тарифам (Старт, Стандарт, Бизнес, Корпоративный).\n"
            "4. Если в контексте действительно нет ответа на вопрос, вежливо скажи, что в регламентах этого нет, "
            "и предложи перевести диалог на менеджера.\n"
            "5. Пиши грамотно, структурировано, без лишнего сухой канцелярита.\n\n"
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
            "source_document": "Ошибка OpenAI API"
        }

# --- Telegram Бот Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Здравствуйте! Я виртуальный ассистент СкладПро.</b>\n\n"
        "Я могу ответить на ваши вопросы по:\n"
        "• Тарифам, стоимости и демо-доступу на 14 дней\n"
        "• Интеграции с 1С и настройке остатков\n"
        "• Учёту партий, срокам годности и мобильному приложению\n\n"
        "Задайте ваш вопрос простыми словами!",
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

    # База Знаний СкладПро
    await update.message.reply_text(f"📚 Ищу ответ в базе знаний: <i>\"{html.escape(query)}\"</i>...", parse_mode="HTML")
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
                    f"🤖 <b>Ответ ассистента:</b>\n\n"
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
        print("🤖 Telegram бот СкладПро успешно запущен!")

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
    """Эндпоинт обращения к Базе Знаний для поиска по регламентам СкладПро"""
    result = await search_via_mcp_agent(question)
    return result

# Точка входа для локального запуска
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
