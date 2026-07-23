import os
import time
import asyncio
from typing import Dict, List
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.security import APIKeyHeader
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# MCP & OpenAI импорты
from openai import AsyncOpenAI
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

# --- Конфигурация и Секреты ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API_URL = os.getenv("API_URL", "https://my-check-bot.onrender.com").rstrip("/")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Путь к папке с регламентами и документами
DOCS_FOLDER_PATH = os.getenv("DOCS_FOLDER_PATH", "./knowledge_docs")

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

# --- MCP Клиент и Логика Поиска ---
async def search_via_mcp_agent(question: str) -> Dict[str, str]:
    """
    Агент подключается к MCP-серверу файловой системы / базы знаний,
    извлекает контекст из документов и генерирует ответ.
    """
    # 1. Параметры запуска официального MCP FileSystem сервера (Node.js)
    # Или аналогичного Python MCP-сервера
    server_params = StdioServerParameters(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", os.path.abspath(DOCS_FOLDER_PATH)],
        env=None
    )

    if not OPENAI_API_KEY:
        # Режим заглушки/фоллбэка, если ключ OpenAI еще не задан
        return {
            "question": question,
            "answer": f"Для вопроса «{question}» по регламенту необходимо подать заявку в отдел кадров за 3 рабочих дня.",
            "source_document": "Регламент_2026.pdf (Стр. 5, п. 2.1)"
        }

    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Получаем доступные MCP-инструменты (search_files, read_file и т.д.)
                tools_response = await session.list_tools()
                mcp_tools = tools_response.tools

                # Преобразуем MCP tools в формат OpenAI Function Calling
                openai_tools = [
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.inputSchema
                        }
                    }
                    for tool in mcp_tools
                ]

                messages = [
                    {
                        "role": "system",
                        "content": (
                            "Ты — корпоративный ассистент компании. Отвечай на вопросы сотрудников, "
                            "строго используя информацию из документов базы знаний через доступные MCP инструменты. "
                            "В конце ответа ВСЕГДА указывай имя файла и источник."
                        )
                    },
                    {"role": "user", "content": question}
                ]

                # Запрос к LLM с инструментами MCP
                response = await openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=messages,
                    tools=openai_tools,
                    tool_choice="auto"
                )

                response_message = response.choices[0].message

                # Если модель решила вызвать MCP инструмент для чтения файлов
                if response_message.tool_calls:
                    messages.append(response_message)
                    for tool_call in response_message.tool_calls:
                        tool_name = tool_call.function.name
                        tool_args = tool_call.function.arguments

                        # Вызываем инструмент напрямую через MCP сессию
                        result = await session.call_tool(tool_name, eval(tool_args))

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": str(result.content)
                        })

                    # Итоговый ответ с учетом вызова инструментов
                    final_response = await openai_client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=messages
                    )
                    answer_text = final_response.choices[0].message.content
                else:
                    answer_text = response_message.content

                return {
                    "question": question,
                    "answer": answer_text,
                    "source_document": "База знаний компании (MCP Filesystem)"
                }

    except Exception as e:
        print(f"Ошибка MCP Агента: {e}")
        return {
            "question": question,
            "answer": f"Произошла ошибка при поиске через MCP: {e}",
            "source_document": "Ошибка MCP"
        }

# --- Telegram Бот Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Привет! Я твой виртуальный ассистент.**\n\n"
        "1️⃣ **Проверка контрагентов:** Отправь мне **ИНН** (10 или 12 цифр).\n"
        "2️⃣ **База знаний (MCP):** Задай любой вопрос по регламентам и документам.",
        parse_mode="Markdown"
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
        await update.message.reply_text(f"🔍 Анализирую запрос по ИНН: `{query}`...", parse_mode="Markdown")
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
                        f"🏢 **Результат проверки**\n\n"
                        f"📌 **Компания:** {data.get('company_name', 'Н/Д')}\n"
                        f"🔢 **ИНН:** `{data.get('inn')}`\n"
                        f"📊 **Уровень риска:** {data.get('risk_label')}\n"
                    )
                    await update.message.reply_text(reply_text, parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"❌ Ошибка API (код {response.status_code}).")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка связи с сервером: {e}")
        
        return  # <-- ВАЖНО: завершаем выполнение, чтобы не идти в Базу Знаний!

    # ==========================================
    # ВЕТКА 2: База Знаний (MCP)
    # ==========================================
    await update.message.reply_text(f"📚 Ищу ответ в регламентах через MCP: *\"{query}\"*...", parse_mode="Markdown")
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"{API_URL}/api/knowledge/query",
                params={"question": query},
                headers=headers
            )
            if response.status_code == 200:
                data = response.json()
                answer = data.get("answer", "Не удалось найти ответ.")
                source = data.get("source_document", "Источник не указан")

                reply_text = (
                    f"🤖 **Ответ из базы знаний (MCP):**\n\n"
                    f"{answer}\n\n"
                    f"📄 **Источник:** `{source}`"
                )
                await update.message.reply_text(reply_text, parse_mode="Markdown")
            else:
                await update.message.reply_text(f"❌ Ошибка MCP API (код {response.status_code}).")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка связи с сервером: {e}")

# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Создаем папку для документов, если её еще нет
    if not os.path.exists(DOCS_FOLDER_PATH):
        os.makedirs(DOCS_FOLDER_PATH, exist_ok=True)

    bot_app = None
    if TELEGRAM_BOT_TOKEN:
        bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        bot_app.add_handler(CommandHandler("start", start_command))
        bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling()
        print("🤖 Telegram бот с поддержкой MCP успешно запущен!")

    yield

    if bot_app:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()

# --- FastAPI App ---
app = FastAPI(title="Company Risk & MCP Knowledge API", lifespan=lifespan)

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Сервис проверки контрагентов и MCP База Знаний работают!"}

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
    """Эндпоинт обращения к MCP-серверу для поиска по регламентам"""
    result = await search_via_mcp_agent(question)
    return result
