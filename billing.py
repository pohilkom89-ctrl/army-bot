import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from yookassa import Configuration, Payment

from config import CYCLES, PLANS
from db.repository import create_subscription

# Billing cycle → how long a paid subscription lasts before it needs renewal.
# Not in config.py because it's an infrastructure concern, not a pricing one.
CYCLE_DURATIONS: dict[str, timedelta] = {
    "monthly": timedelta(days=30),
    "yearly": timedelta(days=365),
}

# Token bucket resets every 30 days regardless of billing cycle — a yearly
# subscriber still gets a monthly refill.
TOKEN_RESET_PERIOD = timedelta(days=30)

_configured = False


def _configure() -> None:
    global _configured
    if _configured:
        return
    shop_id = os.getenv("YUKASSA_SHOP_ID")
    secret_key = os.getenv("YUKASSA_SECRET_KEY")
    if not shop_id or not secret_key:
        raise RuntimeError(
            "YUKASSA_SHOP_ID and YUKASSA_SECRET_KEY env vars are required"
        )
    Configuration.account_id = shop_id
    Configuration.secret_key = secret_key
    _configured = True


def create_payment(client_id: int, tier: str, cycle: str) -> str:
    if tier not in PLANS:
        raise ValueError(f"Unknown tier: {tier}")
    if cycle not in CYCLES:
        raise ValueError(f"Unknown cycle: {cycle}")
    _configure()

    plan = PLANS[tier]
    price_rub = plan[f"price_{cycle}"]
    amount = f"{price_rub:.2f}"
    cycle_label = "месяц" if cycle == "monthly" else "год"
    description = f'Bot Factory — {plan["name"]} ({cycle_label})'

    return_url = os.getenv("BILLING_RETURN_URL", "https://t.me/")

    payment = Payment.create(
        {
            "amount": {"value": amount, "currency": "RUB"},
            "confirmation": {
                "type": "redirect",
                "return_url": return_url,
            },
            "capture": True,
            "description": description,
            "metadata": {
                "client_id": str(client_id),
                "tier": tier,
                "cycle": cycle,
            },
        },
        uuid.uuid4().hex,
    )
    logger.info(
        "billing: payment created id={} client_id={} tier={} cycle={} amount={}",
        payment.id,
        client_id,
        tier,
        cycle,
        amount,
    )
    return payment.confirmation.confirmation_url


def check_payment(payment_id: str) -> str:
    _configure()
    payment = Payment.find_one(payment_id)
    return payment.status


async def handle_webhook(data: dict[str, Any]) -> None:
    event = data.get("event")
    obj = data.get("object") or {}
    payment_id = obj.get("id")
    status = obj.get("status")
    metadata = obj.get("metadata") or {}

    logger.info(
        "billing: webhook event={} payment_id={} status={}",
        event,
        payment_id,
        status,
    )

    if event != "payment.succeeded" or status != "succeeded":
        return

    raw_client_id = metadata.get("client_id")
    try:
        client_id = int(raw_client_id)
    except (TypeError, ValueError):
        logger.error(
            "billing: webhook missing/invalid client_id in metadata: {}",
            raw_client_id,
        )
        return

    tier = metadata.get("tier")
    if tier not in PLANS:
        logger.error("billing: webhook unknown tier={}", tier)
        return

    cycle = metadata.get("cycle")
    if cycle not in CYCLE_DURATIONS:
        logger.error("billing: webhook unknown cycle={}", cycle)
        return

    now = datetime.now(timezone.utc)
    expires_at = now + CYCLE_DURATIONS[cycle]

    await create_subscription(
        client_id=client_id,
        payment_id=payment_id,
        plan=cycle,
        status="active",
        started_at=now,
        expires_at=expires_at,
        tier=tier,
        tokens_reset_at=now + TOKEN_RESET_PERIOD,
    )
    logger.info(
        "billing: subscription activated client_id={} tier={} cycle={} expires={}",
        client_id,
        tier,
        cycle,
        expires_at.isoformat(),
    )
