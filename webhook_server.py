import asyncio
import ipaddress
import os

from aiohttp import web
from loguru import logger

from billing import handle_webhook

# YooKassa delivers webhooks from this fixed set of networks. Anything else is
# rejected at the network layer before we touch the payload — defence-in-depth
# against webhook spoofing. Keep in sync with YooKassa's docs.
_YUKASSA_NETWORKS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.ip_network("185.71.76.0/27"),
    ipaddress.ip_network("185.71.77.0/27"),
    ipaddress.ip_network("77.75.153.0/25"),
    ipaddress.ip_network("77.75.156.11/32"),
    ipaddress.ip_network("77.75.156.35/32"),
)


def _is_yukassa_ip(raw_ip: str | None) -> bool:
    if not raw_ip:
        return False
    try:
        addr = ipaddress.ip_address(raw_ip)
    except ValueError:
        return False
    return any(addr in net for net in _YUKASSA_NETWORKS)


async def yukassa_webhook(request: web.Request) -> web.Response:
    peer = request.remote
    if not _is_yukassa_ip(peer):
        logger.warning("webhook: rejected non-allowlisted IP {}", peer)
        return web.Response(status=403, text="forbidden")

    try:
        data = await request.json()
    except Exception:
        logger.exception("webhook: invalid JSON from {}", peer)
        return web.Response(status=400, text="invalid json")

    try:
        await handle_webhook(data)
    except Exception:
        logger.exception("webhook: handler failed")
        return web.Response(status=500, text="handler error")

    return web.Response(status=200, text="ok")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/webhook/yukassa", yukassa_webhook)
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
