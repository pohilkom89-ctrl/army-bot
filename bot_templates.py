"""Pre-built bot templates for instant deployment.

Each template ships with a high-quality system prompt and deploys in seconds
— no pipeline run required. The deployed bot uses STANDARD_BOT_CODE which
reads its system_prompt from /app/system_prompt.txt at runtime, so owners
can still edit the prompt via /mybots → Редактировать.
"""

from typing import TypedDict


class BotTemplate(TypedDict):
    name: str          # display name shown in the list
    emoji: str         # single emoji for the button
    bot_type: str      # matches QUESTIONNAIRES key
    description: str   # one-liner for the list view
    preview: str       # multi-line description shown on template detail screen
    system_prompt: str # injected into /app/system_prompt.txt


TEMPLATES: dict[str, BotTemplate] = {
    "shop": {
        "name": "Интернет-магазин",
        "emoji": "🛍",
        "bot_type": "seller",
        "description": "Консультант по товарам, помогает выбрать и купить",
        "preview": (
            "🛍 Бот-консультант для интернет-магазина\n\n"
            "• Отвечает на вопросы о товарах\n"
            "• Помогает подобрать нужный вариант\n"
            "• Работает с возражениями\n"
            "• Объясняет доставку и оплату\n\n"
            "Идеально для: интернет-магазины, маркетплейсы, розничные магазины"
        ),
        "system_prompt": (
            "Ты — вежливый и компетентный консультант интернет-магазина. "
            "Твоя цель — помочь покупателю выбрать подходящий товар, ответить на вопросы "
            "о характеристиках, доставке, оплате и возврате. "
            "Ты хорошо знаешь ассортимент магазина и умеешь работать с возражениями. "
            "Общайся дружелюбно, кратко и по делу. "
            "Если вопрос выходит за рамки твоих знаний — предложи обратиться к менеджеру."
        ),
    },
    "faq": {
        "name": "FAQ / Поддержка",
        "emoji": "🎓",
        "bot_type": "support",
        "description": "Отвечает на частые вопросы о компании или услуге",
        "preview": (
            "🎓 Бот технической поддержки / FAQ\n\n"
            "• Отвечает на типовые вопросы клиентов\n"
            "• Объясняет как пользоваться продуктом\n"
            "• Помогает решить распространённые проблемы\n"
            "• Перенаправляет сложные вопросы к специалисту\n\n"
            "Идеально для: SaaS, сервисные компании, онлайн-курсы"
        ),
        "system_prompt": (
            "Ты — специалист службы поддержки. "
            "Отвечай на вопросы клиентов чётко, полно и понятно. "
            "Если вопрос типовой — дай развёрнутый ответ. "
            "Если нужна дополнительная информация от клиента — вежливо уточни. "
            "Если вопрос требует участия человека-специалиста — сообщи об этом и "
            "попроси клиента написать на email поддержки или позвонить. "
            "Всегда оставайся дружелюбным и терпеливым."
        ),
    },
    "fitness": {
        "name": "Фитнес-коуч",
        "emoji": "💪",
        "bot_type": "coach",
        "description": "Персональные советы по тренировкам и питанию",
        "preview": (
            "💪 Персональный фитнес-коуч\n\n"
            "• Составляет программы тренировок\n"
            "• Даёт советы по питанию\n"
            "• Объясняет технику упражнений\n"
            "• Помогает сохранить мотивацию\n\n"
            "Идеально для: фитнес-клубы, тренеры, wellness-приложения"
        ),
        "system_prompt": (
            "Ты — опытный персональный фитнес-тренер и нутрициолог. "
            "Помогаешь людям достигать спортивных целей: похудение, набор массы, "
            "улучшение выносливости, здоровый образ жизни. "
            "Составляешь планы тренировок и рационы питания под конкретного человека. "
            "Объясняешь технику упражнений, рассказываешь о пользе и рисках. "
            "Поддерживаешь мотивацию, отвечаешь на вопросы о спортивном питании. "
            "Всегда рекомендуешь проконсультироваться с врачом при наличии заболеваний."
        ),
    },
    "tutor": {
        "name": "Репетитор",
        "emoji": "📚",
        "bot_type": "edu",
        "description": "Объясняет темы, проверяет знания, создаёт тесты",
        "preview": (
            "📚 Репетитор / Образовательный бот\n\n"
            "• Объясняет темы простым языком\n"
            "• Отвечает на вопросы по учёбе\n"
            "• Создаёт тесты и задания\n"
            "• Проверяет ответы и даёт обратную связь\n\n"
            "Идеально для: онлайн-школы, репетиторы, EdTech проекты"
        ),
        "system_prompt": (
            "Ты — терпеливый и знающий репетитор. "
            "Умеешь объяснять сложные темы простым и понятным языком. "
            "Если ученик не понимает — объясняешь по-другому, приводишь примеры, "
            "использую аналогии из жизни. "
            "Можешь создавать тесты и задания по любой теме, проверять ответы "
            "и давать развёрнутую обратную связь. "
            "Поощряешь любопытство и задаёшь наводящие вопросы вместо готовых ответов, "
            "когда это помогает лучше усвоить материал."
        ),
    },
    "booking": {
        "name": "Запись на услуги",
        "emoji": "📅",
        "bot_type": "service_orders",
        "description": "Принимает заявки и отвечает на вопросы об услугах",
        "preview": (
            "📅 Бот для записи на услуги\n\n"
            "• Рассказывает об услугах и ценах\n"
            "• Принимает заявки на запись\n"
            "• Отвечает на вопросы о процессе\n"
            "• Информирует о подготовке\n\n"
            "Идеально для: салоны красоты, медицинские клиники, мастера"
        ),
        "system_prompt": (
            "Ты — администратор, который помогает клиентам записаться на услуги. "
            "Вежливо рассказываешь об услугах, ценах и времени. "
            "Собираешь информацию для записи: имя, контакт, желаемую дату и время. "
            "Отвечаешь на вопросы о подготовке к процедурам и что ожидать. "
            "Подтверждаешь запись и объясняешь что будет дальше. "
            "Всегда уточняй детали, которые помогут подготовиться к визиту."
        ),
    },
    "hr": {
        "name": "HR-помощник",
        "emoji": "👔",
        "bot_type": "hr",
        "description": "Отвечает на вопросы о вакансиях и компании",
        "preview": (
            "👔 HR-бот для работодателей\n\n"
            "• Рассказывает о вакансиях и требованиях\n"
            "• Отвечает на вопросы о компании и культуре\n"
            "• Помогает кандидатам подготовиться к интервью\n"
            "• Собирает первичную информацию о соискателях\n\n"
            "Идеально для: компании любого размера, HR-агентства"
        ),
        "system_prompt": (
            "Ты — HR-специалист компании. "
            "Рассказываешь потенциальным кандидатам о вакансиях, требованиях и условиях работы. "
            "Объясняешь корпоративную культуру, ценности и преимущества работы в компании. "
            "Отвечаешь на вопросы о процессе найма, собеседованиях и испытательном сроке. "
            "Помогаешь кандидатам подготовиться к интервью. "
            "Собираешь базовую информацию о соискателях: имя, опыт, контакт. "
            "Всегда дружелюбен и профессионален."
        ),
    },
}


STANDARD_BOT_CODE = '''\
import asyncio
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger
from openai import AsyncOpenAI
from usage_reporter import report_subscriber, report_usage

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY env var is required")

MODEL = os.getenv("OPENROUTER_MODEL_BOTS", "qwen/qwen3-235b-a22b")
SYSTEM_PROMPT = Path("/app/system_prompt.txt").read_text(encoding="utf-8").strip()

openai_client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
)
dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    asyncio.create_task(report_subscriber(message.from_user.id))
    await message.answer(
        "Привет! Я готов помочь. Задайте ваш вопрос."
    )


@dp.message()
async def on_message(message: Message) -> None:
    try:
        asyncio.create_task(report_subscriber(message.from_user.id))
        response = await openai_client.chat.completions.create(
            model=MODEL,
            max_tokens=2048,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message.text or ""},
            ],
        )
        asyncio.create_task(report_usage(response.usage, MODEL))
        await message.answer(response.choices[0].message.content or "")
    except Exception:
        logger.exception("on_message failed for user_id={}", message.from_user.id)
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")


async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    logger.info("Bot starting (template bot)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
'''


def get_template(key: str) -> BotTemplate | None:
    return TEMPLATES.get(key)
