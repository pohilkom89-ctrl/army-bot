"""HTML minisite generator for deployed client bots.

Reads bot name, type, and questionnaire answers from BotConfig, then
produces a self-contained HTML page compatible with the Telegram Web App SDK.

Accent colour is a single hex value (e.g. "#e87724"); everything else uses
a shared dark base so only one variable changes per theme.
"""

import html
import re
from typing import Optional

from aiogram import Bot as AiogramBot
from loguru import logger

from db.models import BotConfig
from templates.bot_questionnaires import QUESTIONNAIRES

# 12 preset accent colours shown in the setup wizard.
# "hex" is the accent colour; bg/card stay dark for all.
ACCENT_PRESETS: dict[str, dict[str, str]] = {
    "red":    {"label": "🔴 Красный",     "hex": "#e74c3c"},
    "pink":   {"label": "🌸 Розовый",     "hex": "#e91e8c"},
    "orange": {"label": "🟠 Оранжевый",   "hex": "#e87724"},
    "amber":  {"label": "🟡 Янтарный",    "hex": "#f39c12"},
    "green":  {"label": "🟢 Зелёный",     "hex": "#27ae60"},
    "teal":   {"label": "🩵 Бирюзовый",   "hex": "#1abc9c"},
    "blue":   {"label": "🔵 Синий",       "hex": "#3498db"},
    "indigo": {"label": "💙 Индиго",      "hex": "#3f51b5"},
    "purple": {"label": "🟣 Фиолетовый",  "hex": "#9b59b6"},
    "brown":  {"label": "🟤 Коричневый",  "hex": "#a0522d"},
    "gray":   {"label": "🩶 Серый",       "hex": "#7f8c8d"},
    "silver": {"label": "⚪ Жемчужный",   "hex": "#bdc3c7"},
}

# Shared dark-mode base (unchanged across all accent colours).
_BASE = {
    "bg":         "#0d0d0d",
    "card_bg":    "#1a1a1a",
    "tab_bg":     "#111111",
    "hero_from":  "#111111",
    "border":     "#222222",
    "muted":      "#aaaaaa",
    "label":      "#555555",
}

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")

_TYPE_EMOJI: dict[str, str] = {
    "seller":         "🛒",
    "support":        "💬",
    "service_orders": "📋",
    "coach":          "🏆",
    "lms":            "📚",
    "marketplace":    "🏪",
    "content":        "✍️",
    "parser":         "🔍",
    "real_estate":    "🏠",
    "events":         "🎉",
    "hr":             "👥",
    "quiz":           "❓",
    "finance":        "💰",
    "assistant":      "🤖",
    "planner":        "📅",
    "creative":       "🎨",
    "edu":            "🎓",
    "custom":         "⚙️",
}

_PRICE_RE = re.compile(r"\d[\d\s]*[₽р]")


def is_valid_hex(value: str) -> bool:
    return bool(_HEX_RE.match(value.strip()))


def resolve_accent(value: str) -> str:
    """Turn a preset key OR a hex string into a hex colour."""
    v = value.strip()
    if v in ACCENT_PRESETS:
        return ACCENT_PRESETS[v]["hex"]
    if is_valid_hex(v):
        return v
    return ACCENT_PRESETS["orange"]["hex"]


def accent_label(value: str) -> str:
    """Human-readable label for the chosen accent."""
    v = value.strip()
    if v in ACCENT_PRESETS:
        return ACCENT_PRESETS[v]["label"]
    if is_valid_hex(v):
        return f"✏️ {v.upper()}"
    return ACCENT_PRESETS["orange"]["label"]


def _h(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


async def _get_bot_username(token: str) -> Optional[str]:
    try:
        bot = AiogramBot(token=token)
        try:
            me = await bot.get_me()
            return me.username
        finally:
            await bot.session.close()
    except Exception:
        logger.warning("miniapp: getMe failed — username absent")
        return None


def _parse_services(text: str) -> list[dict]:
    services = []
    for line in text.strip().splitlines():
        line = line.strip(" •-–—*")
        if not line or len(line) < 3:
            continue
        parts = re.split(r"\s*[-–—:]\s*", line, maxsplit=1)
        if len(parts) == 2 and _PRICE_RE.search(parts[1]):
            services.append({"name": parts[0].strip(), "price": parts[1].strip()})
        elif _PRICE_RE.search(line):
            services.append({"name": line, "price": ""})
    return services[:8]


def _extract_data(bot: BotConfig) -> dict:
    config = bot.config_json or {}
    answers = config.get("questionnaire_answers", {})
    ans: dict[str, str] = {str(k): str(v) for k, v in answers.items()} if answers else {}

    q_info = QUESTIONNAIRES.get(bot.bot_type, {})
    type_name: str = q_info.get("name", bot.bot_type)

    q1 = ans.get("1", "")
    q2 = ans.get("2", "")

    return {
        "bot_name": bot.bot_name,
        "type_name": type_name,
        "emoji": _TYPE_EMOJI.get(bot.bot_type, "🤖"),
        "company": q1.split("\n")[0].split(".")[0].strip()[:80] if q1 else "",
        "description": q1[:280] if q1 else type_name,
        "services": _parse_services(q2) if q2 else [],
        "platform": config.get("platform", "telegram"),
    }


def _services_block(services: list[dict], accent: str) -> str:
    if not services:
        return (
            '<p style="color:#666;font-size:14px;text-align:center;padding:32px 0">'
            "Напишите нам, чтобы узнать об услугах и ценах</p>"
        )
    rows = []
    for s in services:
        price = (
            f'<div class="svc-price" style="color:{accent}">{_h(s["price"])}</div>'
            if s["price"] else ""
        )
        rows.append(
            f'<div class="svc-item">'
            f'<div class="svc-name">{_h(s["name"])}</div>{price}</div>'
        )
    return "\n".join(rows)


def _contact_block(username: Optional[str], platform: str, accent: str) -> str:
    if platform == "vk":
        return '<p style="color:#aaa;font-size:14px;text-align:center;padding:8px 0">Напишите нам во ВКонтакте</p>'
    if username:
        return (
            f'<a href="tg://resolve?domain={_h(username)}" '
            f'style="display:block;width:100%;border-radius:10px;padding:14px;'
            f'font-size:15px;font-weight:700;text-align:center;text-decoration:none;'
            f'background:{accent};color:#fff;margin-bottom:8px">'
            f'✈️ Написать в Telegram</a>'
        )
    return '<p style="color:#aaa;font-size:14px;text-align:center;padding:8px 0">Найдите нас в Telegram</p>'


async def generate_miniapp_html(
    bot: BotConfig,
    accent: str = "orange",
    logo_url: Optional[str] = None,
) -> str:
    """Return full HTML for the bot's public minisite.

    Args:
        bot: BotConfig ORM object.
        accent: preset key (e.g. "orange") OR any hex string (e.g. "#FF5733").
        logo_url: relative path to uploaded logo ("logo.jpg") or None → show emoji.
    """
    acc = resolve_accent(accent)
    b = _BASE
    data = _extract_data(bot)

    username: Optional[str] = None
    if data["platform"] == "telegram":
        username = await _get_bot_username(bot.bot_token)

    services_html = _services_block(data["services"], acc)
    contact_html = _contact_block(username, data["platform"], acc)

    hero_visual = (
        f'<div style="width:80px;height:80px;margin:0 auto 12px;border-radius:50%;'
        f'overflow:hidden;border:2px solid {acc}">'
        f'<img src="{_h(logo_url)}" style="width:100%;height:100%;object-fit:cover" alt="logo">'
        f'</div>'
        if logo_url
        else f'<div style="font-size:52px;margin-bottom:10px;line-height:1">{data["emoji"]}</div>'
    )

    company_card = (
        f'<div class="card"><div class="ci">🏢</div>'
        f'<div class="cl">Компания / специалист</div>'
        f'<div class="cv">{_h(data["company"])}</div></div>'
        if data["company"] else ""
    )

    tg_card = (
        f'<div class="card"><div class="ci">✈️</div>'
        f'<div class="cl">Telegram</div>'
        f'<div class="cv" style="color:{acc}">@{_h(username)}</div></div>'
        if username else ""
    )

    contact_logo = (
        f'<div style="width:64px;height:64px;border-radius:50%;overflow:hidden;'
        f'border:2px solid {acc};margin:0 auto 8px">'
        f'<img src="{_h(logo_url)}" style="width:100%;height:100%;object-fit:cover"></div>'
        if logo_url
        else f'<div style="font-size:40px;margin-bottom:6px">{data["emoji"]}</div>'
    )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">
<title>{_h(data["bot_name"])}</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     background:{b["bg"]};color:#fff;min-height:100vh;font-size:14px;max-width:480px;margin:0 auto}}
.hero{{background:linear-gradient(135deg,{b["hero_from"]} 0%,{b["bg"]} 100%);
      padding:28px 16px 20px;text-align:center;border-bottom:1px solid {b["border"]}}}
.hero-name{{font-size:22px;font-weight:800;margin-bottom:4px;line-height:1.3}}
.hero-type{{font-size:11px;color:{acc};margin-bottom:10px;
           text-transform:uppercase;letter-spacing:.06em;font-weight:600}}
.hero-desc{{font-size:13px;color:{b["muted"]};line-height:1.6;
           display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}}
.tabs{{display:flex;background:{b["tab_bg"]};border-bottom:1px solid {b["border"]};
      position:sticky;top:0;z-index:9}}
.tab{{flex:1;padding:11px 0;text-align:center;font-size:13px;color:#555;
     cursor:pointer;border-bottom:2px solid transparent;transition:all .15s}}
.tab.active{{color:{acc};border-bottom-color:{acc}}}
.section{{display:none;padding:14px}}
.section.active{{display:block}}
.card{{background:{b["card_bg"]};border-radius:10px;padding:13px;margin-bottom:10px}}
.ci{{font-size:18px;margin-bottom:3px}}
.cl{{font-size:11px;color:{b["label"]};margin-bottom:2px}}
.cv{{font-size:14px;font-weight:600;line-height:1.5}}
.svc-item{{background:{b["card_bg"]};border-radius:10px;padding:13px;margin-bottom:10px;
          display:flex;justify-content:space-between;align-items:center;gap:10px}}
.svc-name{{font-size:14px;font-weight:500;flex:1}}
.svc-price{{font-size:14px;font-weight:700;white-space:nowrap}}
.powered{{text-align:center;font-size:11px;color:#333;padding:8px 0}}
</style>
</head>
<body>
<div class="hero">
  {hero_visual}
  <div class="hero-name">{_h(data["bot_name"])}</div>
  <div class="hero-type">{_h(data["type_name"])}</div>
  <div class="hero-desc">{_h(data["description"])}</div>
</div>
<div class="tabs">
  <div class="tab active" onclick="s('about',this)">О нас</div>
  <div class="tab" onclick="s('services',this)">Услуги</div>
  <div class="tab" onclick="s('contact',this)">Написать</div>
</div>
<div class="section active" id="tab-about">
  <div class="card"><div class="ci">🤖</div><div class="cl">Тип бота</div>
    <div class="cv">{_h(data["type_name"])}</div></div>
  {company_card}
  <div class="card"><div class="ci">ℹ️</div><div class="cl">О нас</div>
    <div class="cv" style="font-weight:400;line-height:1.6">{_h(data["description"])}</div></div>
  {tg_card}
</div>
<div class="section" id="tab-services">{services_html}</div>
<div class="section" id="tab-contact">
  <div class="card" style="text-align:center;margin-bottom:16px">
    {contact_logo}
    <div style="font-size:16px;font-weight:700;margin-bottom:2px">{_h(data["bot_name"])}</div>
    <div style="font-size:12px;color:{b["muted"]}">{_h(data["type_name"])}</div>
  </div>
  {contact_html}
</div>
<div style="height:20px"></div>
<div class="powered">Создан на <a href="https://armybots.ru" style="color:{acc};text-decoration:none">ArmyBots</a></div>
<div style="height:16px"></div>
<script>
function s(id,el){{
  document.querySelectorAll('.section').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.getElementById('tab-'+id).classList.add('active');el.classList.add('active');
}}
var twa=window.Telegram&&window.Telegram.WebApp;
if(twa){{twa.ready();twa.expand();}}
</script>
</body>
</html>"""
