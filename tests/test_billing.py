"""Webhook authenticity layer (tech debt 9 + 20) and idempotency
(handle_webhook). IP-allowlist is pure-function, easy to verify
exhaustively. verify_payment_status and handle_webhook need mocks for
YooKassa SDK and DB respectively."""
import asyncio

import pytest


# ──── _is_yukassa_ip — IP allowlist ─────────────────────────────────


@pytest.mark.parametrize(
    "ip",
    [
        "185.71.76.5",       # 185.71.76.0/27
        "185.71.77.10",      # 185.71.77.0/27
        "77.75.153.50",      # 77.75.153.0/25
        "77.75.154.200",     # 77.75.154.128/25  (added in tech debt 20)
        "77.75.156.11",      # /32 single host
        "77.75.156.35",      # /32 single host
        "2a02:5180::1",      # IPv6 — added in tech debt 20
        "2a02:5180:abcd::1", # deeper IPv6 in same /32
    ],
)
def test_is_yukassa_ip_accepts_documented_ranges(ip):
    """All 7 documented YooKassa subnets must be recognised. Regression
    here means we'd silently 403 legitimate webhooks if YooKassa
    rebalances delivery (root cause of tech debt 20)."""
    from webhook_server import _is_yukassa_ip

    assert _is_yukassa_ip(ip) is True, f"{ip} should be allowlisted"


@pytest.mark.parametrize(
    "ip",
    [
        "8.8.8.8",       # Google DNS
        "127.0.0.1",     # localhost
        "185.71.78.1",   # near-miss outside 185.71.76.0/27 and /77
        "77.75.155.1",   # near-miss between /153/25 and /156/32
        "::1",           # IPv6 localhost
    ],
)
def test_is_yukassa_ip_rejects_other_addresses(ip):
    from webhook_server import _is_yukassa_ip

    assert _is_yukassa_ip(ip) is False, f"{ip} should NOT be allowlisted"


def test_is_yukassa_ip_handles_garbage():
    from webhook_server import _is_yukassa_ip

    assert _is_yukassa_ip(None) is False
    assert _is_yukassa_ip("") is False
    assert _is_yukassa_ip("not an ip") is False


# ──── verify_payment_status ─────────────────────────────────────────


async def test_verify_payment_status_returns_none_without_credentials(monkeypatch):
    """conftest doesn't set YUKASSA_*; without them the function must
    short-circuit to None so testing flow keeps working before merchant
    onboarding."""
    monkeypatch.delenv("YUKASSA_SHOP_ID", raising=False)
    monkeypatch.delenv("YUKASSA_SECRET_KEY", raising=False)

    from billing import verify_payment_status

    result = await verify_payment_status("any-id", "succeeded")
    assert result is None


async def test_verify_payment_status_true_on_match(monkeypatch, mocker):
    monkeypatch.setenv("YUKASSA_SHOP_ID", "test-shop")
    monkeypatch.setenv("YUKASSA_SECRET_KEY", "test-secret")
    mocker.patch("billing.check_payment", return_value="succeeded")

    from billing import verify_payment_status

    result = await verify_payment_status("payment-xyz", "succeeded")
    assert result is True


async def test_verify_payment_status_false_on_mismatch(monkeypatch, mocker):
    """YooKassa returned a different status than the webhook claimed —
    likely spoofed. Caller should reject with 401."""
    monkeypatch.setenv("YUKASSA_SHOP_ID", "test-shop")
    monkeypatch.setenv("YUKASSA_SECRET_KEY", "test-secret")
    mocker.patch("billing.check_payment", return_value="pending")

    from billing import verify_payment_status

    result = await verify_payment_status("payment-xyz", "succeeded")
    assert result is False


async def test_verify_payment_status_none_on_api_error(monkeypatch, mocker):
    """YooKassa SDK raised — graceful skip rather than blocking webhook."""
    monkeypatch.setenv("YUKASSA_SHOP_ID", "test-shop")
    monkeypatch.setenv("YUKASSA_SECRET_KEY", "test-secret")
    mocker.patch(
        "billing.check_payment", side_effect=RuntimeError("yookassa down")
    )

    from billing import verify_payment_status

    result = await verify_payment_status("payment-xyz", "succeeded")
    assert result is None


async def test_verify_payment_status_none_on_timeout(monkeypatch, mocker):
    """5s timeout via asyncio.wait_for — slow YooKassa shouldn't block."""
    monkeypatch.setenv("YUKASSA_SHOP_ID", "test-shop")
    monkeypatch.setenv("YUKASSA_SECRET_KEY", "test-secret")

    def _slow(_):
        import time
        time.sleep(10)  # blocks the to_thread executor for 10s
        return "succeeded"

    mocker.patch("billing.check_payment", side_effect=_slow)
    # Drop the timeout to keep the test fast
    mocker.patch("billing._VERIFY_TIMEOUT_SECONDS", 0.2)

    from billing import verify_payment_status

    result = await verify_payment_status("payment-xyz", "succeeded")
    assert result is None


# ──── handle_webhook idempotency ────────────────────────────────────


async def test_handle_webhook_skips_duplicate_payment(mocker):
    """Tech debt 9 fix: YooKassa retries delivery even on 200; without
    idempotency a single payment would activate two subscriptions."""
    fake_existing = mocker.MagicMock(id=42)
    mocker.patch(
        "billing.find_subscription_by_payment_id", return_value=fake_existing
    )
    mock_create = mocker.patch("billing.create_subscription")

    from billing import handle_webhook

    await handle_webhook(
        {
            "event": "payment.succeeded",
            "object": {
                "id": "duplicate-payment-id",
                "status": "succeeded",
                "metadata": {
                    "client_id": "1",
                    "tier": "starter",
                    "cycle": "monthly",
                },
            },
        }
    )

    # Subscription already exists for this payment_id — must NOT create another
    mock_create.assert_not_called()


async def test_handle_webhook_creates_subscription_for_new_payment(mocker):
    """Mirror of the idempotency test — first delivery for a payment_id
    actually creates the subscription."""
    mocker.patch(
        "billing.find_subscription_by_payment_id", return_value=None
    )
    mock_create = mocker.patch("billing.create_subscription")

    from billing import handle_webhook

    await handle_webhook(
        {
            "event": "payment.succeeded",
            "object": {
                "id": "new-payment-id",
                "status": "succeeded",
                "metadata": {
                    "client_id": "7",
                    "tier": "pro",
                    "cycle": "yearly",
                },
            },
        }
    )

    mock_create.assert_called_once()
    kwargs = mock_create.call_args.kwargs
    assert kwargs["client_id"] == 7
    assert kwargs["tier"] == "pro"
    assert kwargs["plan"] == "yearly"
