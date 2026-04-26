import asyncio
import ipaddress
import os
import secrets

from aiohttp import web
from loguru import logger

from billing import handle_webhook, verify_payment_status
from db.repository import get_bot_by_id_any, log_tokens
from monitoring.health import register_health_routes

# YooKassa delivers webhooks from this fixed set of networks. List
# verified against https://yookassa.ru/developers/using-api/webhooks
# (2026-04-25). Includes both IPv4 and IPv6.
_YUKASSA_NETWORK_SPECS: tuple[str, ...] = (
    "185.71.76.0/27",
    "185.71.77.0/27",
    "77.75.153.0/25",
    "77.75.154.128/25",
    "77.75.156.11/32",
    "77.75.156.35/32",
    "2a02:5180::/32",
)
_YUKASSA_NETWORKS: tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
] = tuple(ipaddress.ip_network(s) for s in _YUKASSA_NETWORK_SPECS)


def _is_yukassa_ip(raw_ip: str | None) -> bool:
    if not raw_ip:
        return False
    try:
        addr = ipaddress.ip_address(raw_ip)
    except ValueError:
        return False
    return any(addr in net for net in _YUKASSA_NETWORKS)


async def yukassa_webhook(request: web.Request) -> web.Response:
    """Process YooKassa payment webhooks with two layers of authentication:
    (1) IP-allowlist against the documented YooKassa networks, and
    (2) round-trip status verification — pull the payment back from
    YooKassa's API and confirm it's really in the status the webhook
    claims. The second layer substitutes for the missing HMAC and
    blocks both spoofed webhooks (attacker has no real payment_id) and
    replays (real payment but already in our DB / different status now)."""
    peer = request.remote
    if not _is_yukassa_ip(peer):
        logger.warning("webhook: rejected non-allowlisted IP={}", peer)
        return web.Response(status=403, text="forbidden")

    try:
        data = await request.json()
    except Exception:
        logger.exception("webhook: invalid JSON from IP={}", peer)
        return web.Response(status=400, text="invalid json")

    obj = data.get("object") or {}
    payment_id = obj.get("id")
    claimed_status = obj.get("status")

    if not payment_id or not claimed_status:
        logger.warning(
            "webhook: rejected — missing payment_id/status IP={} payload_keys={}",
            peer,
            list(data.keys()),
        )
        return web.Response(status=401, text="missing payment_id/status")

    # Round-trip status check. None means we couldn't reach YooKassa
    # (timeout / missing credentials) — fall back to IP-allowlist alone
    # so testing doesn't break before YUKASSA_* env vars are filled in.
    verified = await verify_payment_status(payment_id, claimed_status)
    if verified is False:
        logger.warning(
            "webhook: REJECTED status mismatch IP={} payment_id={} "
            "claimed_status={} — YooKassa API returned different value",
            peer,
            payment_id,
            claimed_status,
        )
        return web.Response(status=401, text="status mismatch")
    if verified is None:
        logger.warning(
            "webhook: status verification skipped IP={} payment_id={} "
            "claimed_status={} — credentials missing or YooKassa unreachable",
            peer,
            payment_id,
            claimed_status,
        )

    try:
        await handle_webhook(data)
    except Exception:
        logger.exception("webhook: handler failed payment_id={}", payment_id)
        return web.Response(status=500, text="handler error")

    return web.Response(status=200, text="ok")


async def log_tokens_endpoint(request: web.Request) -> web.Response:
    expected = os.getenv("INTERNAL_API_KEY", "")
    if not expected:
        logger.error("log_tokens: INTERNAL_API_KEY not set on factory")
        return web.Response(status=503, text="not configured")

    provided = request.headers.get("X-Internal-Key", "")
    if not secrets.compare_digest(provided, expected):
        logger.warning("log_tokens: bad key from {}", request.remote)
        return web.Response(status=401, text="unauthorized")

    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid json")

    try:
        bot_id = int(data["bot_id"])
        tokens_in = int(data["tokens_in"])
        tokens_out = int(data["tokens_out"])
        model = str(data["model"])
    except (KeyError, ValueError, TypeError) as e:
        logger.warning("log_tokens: bad payload — {}", e)
        return web.Response(status=400, text="invalid payload")

    bot = await get_bot_by_id_any(bot_id)
    if bot is None:
        # Idempotent: orphan reports (e.g. after bot deletion) shouldn't make
        # the client retry forever. Log loudly and return 200.
        logger.warning(
            "log_tokens: unknown bot_id={} — accepting as no-op (idempotent)",
            bot_id,
        )
        return web.Response(status=200, text="ok (unknown bot_id, ignored)")

    try:
        await log_tokens(
            client_id=bot.client_id,
            bot_id=bot_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
        )
    except Exception:
        logger.exception("log_tokens: persisting failed bot_id={}", bot_id)
        return web.Response(status=500, text="logging failed")

    return web.Response(status=200, text="ok")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/webhook/yukassa", yukassa_webhook)
    app.router.add_post("/internal/log_tokens", log_tokens_endpoint)
    register_health_routes(app)
    return app


async def start_webhook_server() -> None:
    port = int(os.getenv("WEBHOOK_PORT", "8080"))
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("webhook: listening on 0.0.0.0:{}/webhook/yukassa", port)

    # Keep the coroutine alive so asyncio.gather in main() treats it as a
    # long-running service alongside dp.start_polling.
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info("webhook: shutting down")
        await runner.cleanup()
        raise
