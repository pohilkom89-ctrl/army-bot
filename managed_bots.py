"""Raw HTTP helpers for Telegram Managed Bots API (Bot API 9.6).

These bypass aiogram 3.13 (which predates the managed-bot types) and call
the Telegram Bot API directly via aiohttp.
"""

import json

import aiohttp

_TG_API = "https://api.telegram.org"


async def send_managed_bot_button(
    bot_token: str,
    chat_id: int,
    request_id: int,
    suggested_name: str | None = None,
    suggested_username: str | None = None,
) -> None:
    """Send a ReplyKeyboard with a KeyboardButtonRequestManagedBot button.

    When the user taps the button, Telegram creates a managed bot and the
    managing bot receives a ManagedBotUpdated update (handled by
    ManagedBotMiddleware in main.py).
    """
    managed_bot_field: dict = {"request_id": request_id}
    if suggested_name:
        managed_bot_field["suggested_name"] = suggested_name
    if suggested_username:
        managed_bot_field["suggested_username"] = suggested_username

    payload = {
        "chat_id": chat_id,
        "text": (
            "Нажмите кнопку — Telegram создаст бота автоматически без BotFather.\n\n"
            "Или введите токен от @BotFather вручную."
        ),
        "reply_markup": json.dumps({
            "keyboard": [[{
                "text": "🤖 Создать бота автоматически",
                "request_managed_bot": managed_bot_field,
            }]],
            "one_time_keyboard": True,
            "resize_keyboard": True,
        }),
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{_TG_API}/bot{bot_token}/sendMessage",
            data=payload,
        ) as resp:
            body = await resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"send_managed_bot_button failed: {body.get('description')}")


async def delete_managed_bot(managing_bot_token: str, bot_user_id: int) -> bool:
    """Call deleteManagedBot to permanently delete a managed bot from Telegram.

    Returns True on success, False if Telegram returned an error (e.g. bot was
    created manually via BotFather and is not a managed bot).
    """
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{_TG_API}/bot{managing_bot_token}/deleteManagedBot",
            json={"user_id": bot_user_id},
        ) as resp:
            body = await resp.json()
    return bool(body.get("ok"))


async def get_managed_bot_token(managing_bot_token: str, bot_user_id: int) -> str:
    """Call getManagedBotToken and return the child bot's token string."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{_TG_API}/bot{managing_bot_token}/getManagedBotToken",
            json={"user_id": bot_user_id},
        ) as resp:
            body = await resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"getManagedBotToken failed: {body.get('description')}")
    return body["result"]
