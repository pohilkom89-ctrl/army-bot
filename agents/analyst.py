import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from pipeline import run_agent

logger = logging.getLogger(__name__)


ANALYST_SYSTEM_PROMPT = """Ты — бизнес-аналитик фабрики Telegram-ботов.
Клиент выбрал тип бота и ответил на анкету из 10-13 конкретных вопросов.
Твоя задача — извлечь из ответов структурированные требования и type-specific данные.

Верни СТРОГО валидный JSON-объект. Никакого текста до или после JSON.
Никаких markdown-блоков, никаких ```json``` обёрток, никаких пояснений.

Схема ответа:
{
  "bot_type": "parser" | "seller" | "content" | "support" | "service_orders" | "coach" | "creative" | "planner" | "edu" | "hr",
  "purpose": "краткое описание цели бота (1-2 предложения)",
  "target_audience": "кто будет использовать бота",
  "key_features": ["фича1", "фича2", ...],
  "tone": "formal" | "friendly" | "professional",
  "language": "ru" | "en",
  "complexity": "simple" | "medium" | "complex",
  "extras": { ...type-specific структурированные данные... }
}

Правила классификации bot_type:
- "parser"         — собирает/парсит данные из внешних источников (VK, TG, Instagram)
- "seller"         — продаёт ТОВАРЫ (физические или цифровые), принимает заказы на товар
- "content"        — генерирует тексты, посты, статьи
- "support"        — отвечает на готовые вопросы из FAQ/базы знаний (статичные ответы)
- "service_orders" — записывает клиентов на УСЛУГИ к конкретному мастеру/времени (барбершоп, СПА, мастер маникюра, автосервис). Корзина услуг + расписание + персонал + опциональная предоплата
- "coach"          — ведёт клиента ПО ДОЛГОСРОЧНОЙ ПРОГРАММЕ (фитнес-тренер, лайф-коуч, бизнес-наставник, нутрициолог). Прогресс по этапам, ежедневные задания, мотивация, отслеживание показателей
- "creative"       — помогает ПРИДУМЫВАТЬ ИДЕИ через методики мышления (Six Hats, SCAMPER, Mind Map, Design Thinking). Брейншторм, нейминг, концепции рекламы, питчи, контент-стратегия. Для маркетологов/копирайтеров/продактов/агентств. Часто с памятью контекста между сессиями
- "planner"        — управляет ЗАДАЧАМИ И ВРЕМЕНЕМ пользователя (списки дел, привычки, цели, напоминания, аналитика выполнения, GTD/Bullet Journal/Pomodoro). Универсальный продуктивности-бот, не привязан к конкретной программе обучения. Часто с расписанными напоминаниями и streak-счётчиками
- "edu"            — ПРЕПОДАЁТ ПРЕДМЕТ через структурированный курс уроков с тестами, домашними заданиями и проверкой знаний (английский, математика, программирование, маркетинг, дизайн). Уроки → теория → примеры → практика → тест → ДЗ. Прогресс по уровням (A1-C2 / начинающий-продвинутый / по классам). Часто с сертификатами и геймификацией streak/баллы
- "hr"             — НАНИМАЕТ КАНДИДАТОВ через автоматизированную воронку рекрутинга (скрининг резюме/анкет → тесты на знания → видео-интервью → передача прошедших HR-менеджеру или руководителю). Для компаний с большим объёмом найма. Этапы funnel'а: заявка → screening → тест → интервью → оффер → онбординг. Часто с базой знаний о компании, обработкой отказов и уведомлениями в Telegram/email/CRM

Различия похожих типов (ВАЖНО — не путать):
- seller vs service_orders — продаёт ТОВАР (его привезут/отдадут) → seller; продаёт УСЛУГУ С ЗАПИСЬЮ на конкретный слот к конкретному мастеру → service_orders. Если есть «график мастеров» / «свободные часы» / «бронирование» — это service_orders, даже если бизнес называет это «продажа».
- support vs coach — отвечает разово на вопросы из FAQ → support; ведёт клиента по плану несколько недель/месяцев с трекингом прогресса → coach
- content vs coach — генерирует ТЕКСТЫ для публикации → content; персональная программа с заданиями для клиента → coach
- content vs creative — генерирует ГОТОВЫЙ публикационный артефакт (пост, статью, рассылку с конкретным текстом) → content; помогает ПРИДУМАТЬ ИДЕИ/КОНЦЕПЦИИ/НАПРАВЛЕНИЯ (списки вариантов, нейминг, питчи, методики мышления) → creative. Если запрос «напиши пост на тему X» → content; если «придумай 20 идей постов» или «помоги с концепцией кампании» → creative
- creative vs coach — креатив помогает с КРЕАТИВНЫМИ ЗАДАЧАМИ (одноразовые сессии брейншторма) → creative; ведёт клиента ПО ПРОГРАММЕ с прогрессом → coach
- planner vs coach — coach ведёт по КОНКРЕТНОЙ ОБУЧАЮЩЕЙ/ТРЕНИРОВОЧНОЙ ПРОГРАММЕ с заранее заданным контентом и этапами (Похудение за 30 дней, Курс по Python) → coach; planner — ОБЩАЯ ЛИЧНАЯ ПРОДУКТИВНОСТЬ пользователя без жёстко заданной программы (списки дел, привычки, цели, GTD) → planner. Если бот «знает программу и ведёт по ней» → coach; если бот «помогает упорядочить ЛЮБЫЕ задачи пользователя» → planner
- planner vs support — support отвечает на вопросы клиентов КОМПАНИИ из её FAQ → support; planner управляет ЛИЧНЫМИ задачами/привычками самого пользователя → planner
- edu vs coach — coach ведёт по НАВЫКОВОЙ ПРОГРАММЕ (фитнес, питание, лайф-цели) с прогрессом по показателям/замерам, ежедневной мотивацией и адаптацией под клиента; edu преподаёт ПРЕДМЕТ через стандартизированный курс (уроки, тесты, ДЗ, оценки), один и тот же контент для всех учеников. «Учить ДЕЛАТЬ» (тренировать привычку, доводить до результата) → coach; «Учить ЗНАТЬ» (передавать знания с проверкой усвоения) → edu. Если есть «уроки», «тесты», «ДЗ», «уровни A1-C2/по классам», «сертификаты по итогу» → edu, даже если бизнес называет это «курсом коучинга»
- edu vs content — content генерирует ТЕКСТЫ для публикации; edu проводит УРОКИ с тестами и проверкой знаний. Если бот «генерирует контент к уроку» — это всё равно edu (учебный материал — часть курса), не content
- edu vs support — support отвечает разово на вопросы из FAQ компании; edu ведёт ученика по СТРУКТУРИРОВАННОМУ курсу с уроками и проверкой знаний
- hr vs support — support отвечает на вопросы КЛИЕНТОВ компании из её FAQ (товары, услуги, доставка); hr общается с КАНДИДАТАМИ внутри воронки найма (заявка → тест → интервью → оффер) и принимает решения о их продвижении дальше. Если бот «помогает клиенту» → support; если бот «оценивает кандидата» → hr
- hr vs edu — edu ОБУЧАЕТ ученика и проверяет усвоение материала; hr ТЕСТИРУЕТ кандидата на готовые знания/навыки для принятия решения о найме (тесты — инструмент скрининга, не обучения, и они ИДУТ ОДНОКРАТНО, без курса). Если есть «уроки», «ДЗ», «уровни» → edu; если есть «вакансии», «кандидаты», «оффер», «воронка» → hr
- hr vs seller — seller продаёт ТОВАР/УСЛУГУ клиенту (компания → клиент); hr нанимает СОТРУДНИКА (компания ↔ кандидат, обратное направление: компания «продаёт себя» соискателю и одновременно оценивает его). Если есть «прайс», «корзина», «доставка» → seller; если есть «вакансии», «опыт работы», «зарплатные ожидания» → hr
- hr vs service_orders — service_orders записывает клиентов на УСЛУГУ (стрижка, маникюр, консультация) с расписанием мастеров; hr ведёт КАНДИДАТОВ по этапам найма (несколько собеседований растянуты во времени, но это не «запись на услугу»). Если бот бронирует слот к мастеру → service_orders; если бот ведёт кандидата по воронке → hr

Правила по complexity:
- "simple"  — до 3 фич, без внешних интеграций
- "medium"  — 4-7 фич либо 1-2 интеграции
- "complex" — больше 7 фич или несколько сложных интеграций

Правила по key_features:
- минимум один элемент
- формулируй как конкретные действия бота («парсит VK-группы ежедневно», «принимает заявки в Telegram менеджера»)
- НЕ включай в key_features секреты, API-токены или контактные данные

Правила по extras (type-specific структурирование ответов клиента):

- Для parser:
  {
    "vk_sources": ["vk.com/...", ...],          // из вопроса про VK группы
    "telegram_sources": ["@channel1", ...],     // из вопроса про TG каналы
    "keywords": ["слово1", ...],                // из вопроса про ключевые слова
    "niche": "ниша бизнеса",
    "report_frequency": "daily | weekly | on_demand",
    "report_format": "table | text | top5 | ...",
    "has_vk_token": true | false,               // true если клиент указал токен (значение НЕ включай!)
    "generate_articles": true | false,
    "article_style": "...",
    "article_length_words": число | null,
    "publish_to": "..."
  }

- Для seller:
  {
    "company": "название и сфера",
    "products": [{"name": "...", "price": "..."}],  // из списка «товар — цена»
    "goal": "заявка | консультация | онлайн-продажа",
    "faq": [{"q": "...", "a": "..."}],              // если клиент дал пары
    "manager_telegram_placeholder": true | false,   // true если клиент указал контакт (сам контакт НЕ пиши!)
    "delivery_and_payment": "...",
    "discounts": "...",
    "tone": "official | friendly | expert",
    "human_fallback": "switch_to_manager | give_contacts",
    "working_hours": "...",
    "warranty_return": "...",
    "website": "..."
  }

- Для content:
  {
    "niche": "...",
    "audience": "...",
    "content_types": ["post_vk", "article", "stories", "email"],
    "tone_style": "...",
    "example_texts": "...",
    "forbidden_topics": ["..."],
    "post_length": "short | medium | long",
    "use_hashtags": true | false,
    "hashtag_themes": ["..."],
    "frequency": "on_demand | scheduled",
    "publish_to": "...",
    "publish_channel_placeholder": true | false,
    "competitors": ["..."]
  }

- Для support:
  {
    "company": "...",
    "faq": [{"q": "...", "a": "..."}],           // разбери базу знаний на пары
    "top_issues": ["проблема 1", ...],
    "unknown_answer_strategy": "switch_to_human | ask_to_wait",
    "manager_telegram_placeholder": true | false,
    "working_hours": "...",
    "tone": "official | friendly",
    "forbidden_topics": ["..."],
    "collect_contacts": ["name", "phone", "email"] | [],
    "ticket_storage": "telegram | google_sheets | ...",
    "documents_urls": ["..."],
    "welcome_message": "...",
    "peak_hours": "..."
  }

- Для service_orders:
  {
    "company": "название и сфера",
    "location": "адрес или несколько точек",
    "services": [{"name": "...", "price": "...", "duration": "..."}],  // услуга-цена-длительность
    "schedule": "график работы",
    "staff": ["имя1", "имя2", ...] | "число сотрудников",  // имена если бронь к конкретному мастеру
    "prepayment": "none | partial | full",
    "payment_methods": ["...", ...],
    "manager_telegram_placeholder": true | false,    // true если клиент дал контакт (значение НЕ пиши!)
    "booking_close_hours_before": число | null,      // за сколько часов закрывается запись
    "cancellation_policy": "...",
    "promotions": "...",
    "tone": "official | friendly | informal",
    "forbidden_topics": ["..."],
    "reminders": "24h | 2h | both | none",
    "extras_notes": "особенности бизнеса"
  }

- Для coach:
  {
    "niche": "fitness | life_coach | business | nutrition | ...",
    "audience": "ЦА с возрастом, полом, болями",
    "programs": [{"name": "...", "duration": "...", "price": "..."}],  // программа-длительность-цена
    "format": "online | offline | group | individual | mixed",
    "progress_tracking": ["measurements", "photos", "metrics", "checklists", "daily_report"],
    "daily_assignments": "morning | evening | client_schedule | none",
    "motivation_style": "tough | supportive | expert",
    "materials": ["pdf", "video", "audio", "checklists", ...],
    "feedback_frequency": "daily | weekly | end_of_program | none",
    "relapse_strategy": "support_continue | remind_consequences | refer_to_coach",
    "payment_terms": "full_prepay | installments | per_stage",
    "warranty_return": "...",
    "personal_consult_telegram_placeholder": true | false,  // true если клиент дал @ тренера
    "contraindications": ["..."],                    // кому не подходит, противопоказания
    "forbidden_topics": ["..."],
    "approach_notes": "уникальность методики, ограничения"
  }

- Для creative:
  {
    "client_role": "marketer | copywriter | product | agency | ...",   // роль владельца бота
    "task_types": ["ideas", "ads", "content_strategy", "naming", "headlines", "pitches", ...],
    "methodologies": ["six_hats", "scamper", "mind_map", "design_thinking", "any"],
    "output_format": "list | categories | story | with_examples",
    "tone": "expert | provocateur | facilitator | analyst",
    "detail_level": "headlines_only | brief | detailed",
    "industries": ["it", "food", "fashion", "b2b", ...] | "universal",
    "asks_clarifying_questions": "always | when_incomplete | never",
    "criticizes_ideas": "yes_with_reasoning | no_just_lists | on_request",
    "memory_mode": "persistent | session_only | none",   // нужна ли долговременная память клиента
    "forbidden_topics": ["..."],
    "criticism_response": "defend | adapt | inquire",   // как бот реагирует на критику его идей
    "extra_formats": ["mind_map", "table", "pitch_structure"] | [],
    "response_length": "short | medium | long",
    "approach_notes": "уникальные методики, кейсы, ограничения"
  }

- Для planner:
  {
    "user_scope": "personal | team | service_clients | broad_audience",  // кто пользуется ботом
    "task_categories": ["work", "personal", "study", ...] | "by_priority" | "custom",
    "reminder_modes": ["before_deadline", "morning_summary", "evening_review", "on_demand"],
    "input_format": "text | voice | template | natural_language",
    "habits": "daily | weekly | streak_counter | none",
    "long_term_goals": "monthly | quarterly | yearly | with_subtasks | none",
    "analytics": "completion_count | percentage | streak | none",
    "motivation_style": "supportive | tough | neutral | gamified",
    "gamification": "points | levels | achievements | none",
    "task_breakdown": "auto | on_request | none",          // помощь в декомпозиции больших задач
    "overdue_strategy": "reschedule | persistent_remind | delete | ask",
    "daily_rituals": "morning_plan | evening_review | both | none",
    "constraints": ["no_night_reminders", "no_criticism", ...],
    "methodology": "gtd | bullet_journal | pomodoro | custom | none",
    "approach_notes": "особенности подхода"
  }

- Для edu:
  {
    "subject": "english | math | programming | marketing | design | ...",   // что преподаём
    "audience": "kids | school | students | adults | beginners | advanced",
    "levels": "a1_c2 | begin_mid_adv | by_grade | none",
    "lesson_format": "text | video | audio | interactive | mixed",
    "lessons_count": число | "диапазон",          // например 12 или "10-15"
    "lesson_duration": "...",                     // длительность одного урока (15 мин / 30 мин / 1 час)
    "lesson_structure": "theory_examples_practice_test | custom",
    "tests_mode": "after_lesson | end_of_module | final_exam | none",
    "homework": "mandatory | optional | none | bot_practice_only",
    "homework_check": "auto | manual_teacher | self_check_with_reference",
    "error_explanation": "detailed | brief | answer_only | score_only",
    "gamification": ["points", "certificates", "streak", "levels"] | "none",
    "communication_style": "friendly_mentor | strict_teacher | playful_peer",
    "reminders": "daily | scheduled | on_skip | none",
    "teacher_transition": "on_complex | on_request | paid_upgrade_only | none",
    "teacher_telegram_placeholder": true | false,    // true если клиент дал @ препода (значение НЕ копируй!)
    "methodology_notes": "уникальность подхода, гарантии, типичные кейсы"
  }

- Для hr:
  {
    "company": "название и сфера",
    "company_size": "startup | small | medium | enterprise",
    "positions": ["Frontend Dev", "Sales Manager", ...],   // конкретные вакансии или направления
    "hiring_volume": "...",                                  // объём найма (например "20 кандидатов в неделю")
    "funnel_stages": ["application", "screening", "test", "video_interview", "hr_interview", "manager_interview", "offer", "onboarding"],
    "bot_tasks": ["screening", "knowledge_tests", "interview", "offer", "onboarding"],   // что бот закрывает в funnel
    "screening_criteria": ["experience", "salary_expectations", "relocation", "english", ...],
    "tests_mode": "technical | situational | personality | mixed | none",
    "decision_maker": "hr | manager | joint",
    "notification_channel": "telegram | email | crm | mixed",
    "hr_telegram_placeholder": true | false,    // true если клиент дал @ HR/контакт (значение НЕ копируй!)
    "candidate_benefits": ["salary_range", "remote", "dms", "training", "stock_options", ...],   // что бот подсвечивает кандидатам
    "tone": "friendly | professional | informal | strict",
    "rejection_strategy": "polite_auto | silent | reserve_pool",
    "company_kb_provided": true | false,         // дал ли клиент базу знаний о компании (миссия, ценности, истории) — текст в company_kb_summary
    "company_kb_summary": "...",                 // краткое содержание базы знаний (без секретов и пд)
    "forbidden_topics": ["..."],
    "process_notes": "уникальные практики найма, культурные особенности"
  }

КРИТИЧЕСКИ ВАЖНО по секретам:
- Если в ответе клиента встречаются API-токены, пароли, ключи, контакты менеджера (@username, номер телефона) — НЕ копируй их значения в extras. Вместо значения ставь плейсхолдер-флаг (*_placeholder: true или has_*_token: true).
- target_audience/purpose/key_features тоже НЕ должны содержать секретов.
- Если клиент ничего не ответил на вопрос — пропусти соответствующий ключ в extras (не придумывай данные).
- Если клиент не указал язык явно — ставь "ru".
- Если тональность не указана — ставь "friendly"."""


class RequirementsSchema(BaseModel):
    bot_type: Literal[
        "parser",
        "seller",
        "content",
        "support",
        "service_orders",
        "coach",
        "creative",
        "planner",
        "edu",
        "hr",
    ]
    purpose: str = Field(min_length=1)
    target_audience: str = Field(min_length=1)
    key_features: list[str] = Field(min_length=1)
    tone: Literal["formal", "friendly", "professional"]
    language: Literal["ru", "en"]
    complexity: Literal["simple", "medium", "complex"]
    extras: dict[str, Any] = Field(default_factory=dict)


COMPLETENESS_SYSTEM_PROMPT = """Ты проверяешь качество собранных требований для бота.
Оцени насколько полны ответы клиента.
Если каких-то важных деталей не хватает —
верни список из 1-3 уточняющих вопросов.
Если всё достаточно — верни пустой список.

Примеры когда нужны уточнения:
- Продавец не указал цены на товары
- Поддержка не дала FAQ (ответила одним словом)
- Контент-бот не описал целевую аудиторию
- Парсер не дал конкретных ссылок на конкурентов

Верни ТОЛЬКО JSON: {"questions": ["вопрос1", "вопрос2"]}"""


class CompletenessSchema(BaseModel):
    questions: list[str] = Field(default_factory=list)


def _format_requirements_for_check(requirements: dict[str, Any]) -> str:
    """Render the questionnaire answers as a readable Q&A block."""
    lines: list[str] = []
    for qid in sorted(
        requirements.keys(),
        key=lambda k: int(k) if str(k).isdigit() else 0,
    ):
        entry = requirements[qid]
        if isinstance(entry, dict):
            q = entry.get("question", "")
            a = entry.get("answer", "")
            lines.append(f"Q: {q}\nA: {a}")
        else:
            lines.append(f"Q{qid}: {entry}")
    return "\n\n".join(lines)


def check_completeness(requirements: dict[str, Any]) -> list[str]:
    """Ask the analyst LLM whether the client's answers are complete enough.

    Returns up to 3 clarifying questions, or an empty list if no follow-up
    is needed. On any LLM/parse failure, returns [] — clarification is an
    enhancement, not a hard gate, so failure falls through to ask_bot_token.
    """
    logger.info(
        "check_completeness: checking %d answers", len(requirements)
    )
    user_message = (
        "Ответы клиента на анкету:\n\n"
        + _format_requirements_for_check(requirements)
    )

    try:
        raw = run_agent(
            system=COMPLETENESS_SYSTEM_PROMPT, user_message=user_message
        )
        parsed = json.loads(_strip_fence(raw))
        validated = CompletenessSchema.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError) as err:
        logger.warning("check_completeness: invalid output: %s", err)
        return []
    except Exception:
        logger.exception("check_completeness: LLM call failed")
        return []

    questions = [q.strip() for q in validated.questions if q and q.strip()][:3]
    logger.info(
        "check_completeness: %d clarifying questions", len(questions)
    )
    return questions


def _strip_fence(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def analyst_agent(raw_input: str) -> dict[str, Any]:
    logger.info("analyst_agent: processing %d chars", len(raw_input))
    user_message = f"Ответы клиента:\n{raw_input}"

    last_error: Exception | None = None
    for attempt in (1, 2):
        raw = run_agent(system=ANALYST_SYSTEM_PROMPT, user_message=user_message)
        try:
            parsed = json.loads(_strip_fence(raw))
            validated = RequirementsSchema.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError) as err:
            last_error = err
            logger.warning(
                "analyst_agent: invalid output on attempt %d: %s", attempt, err
            )
            user_message = (
                f"Твой предыдущий ответ не прошёл валидацию: {err}\n"
                "Верни СТРОГО валидный JSON по схеме, без markdown-блоков и пояснений.\n\n"
                f"Ответы клиента:\n{raw_input}"
            )
            continue

        logger.info(
            "analyst_agent: ok (bot_type=%s, features=%d, extras_keys=%d, complexity=%s)",
            validated.bot_type,
            len(validated.key_features),
            len(validated.extras),
            validated.complexity,
        )
        return validated.model_dump()

    raise ValueError(
        f"analyst_agent failed validation after retry: {last_error}"
    ) from last_error
