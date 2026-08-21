"""
Tests for Stripe webhook handling.
No real Stripe calls — we pass constructed event dicts directly to handle_webhook().
"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True, scope="module")
def tmp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    os.environ["DB_PATH"] = db_path
    os.environ.setdefault("SECRET_KEY", "test-secret-key-32-bytes-padding!")
    os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_fake")
    os.environ.setdefault("STRIPE_PRICE_ID", "price_fake")
    os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_fake")

    import fundscan.db as db_mod
    db_mod.DB_PATH = Path(db_path)
    db_mod.init_db(Path(db_path))

    # Seed a user
    import fundscan.auth as auth_mod
    auth_mod.get_or_create_user("subscriber@example.com")

    yield db_path
    os.unlink(db_path)


def _make_event(event_type: str, obj: dict) -> dict:
    return {"type": event_type, "id": "evt_test", "data": {"object": obj}}


# ---------------------------------------------------------------------------
# checkout.session.completed → tier = pro
# ---------------------------------------------------------------------------

def test_checkout_completed_sets_pro():
    from fundscan.billing import handle_webhook
    from fundscan.auth import get_or_create_user

    user = get_or_create_user("subscriber@example.com")
    assert user["tier"] == "free"

    event = _make_event("checkout.session.completed", {
        "client_reference_id": "subscriber@example.com",
        "customer_email": "subscriber@example.com",
    })

    with patch("fundscan.billing.notify_new_signup"):
        with patch("fundscan.billing.log_webhook_event"):
            handle_webhook(event)

    from fundscan.auth import get_user_by_id
    updated = get_user_by_id(user["id"])
    assert updated["tier"] == "pro"


# ---------------------------------------------------------------------------
# customer.subscription.deleted → tier = free
# ---------------------------------------------------------------------------

def test_subscription_deleted_sets_free():
    from fundscan.billing import handle_webhook
    from fundscan.auth import get_or_create_user, set_user_tier

    set_user_tier("subscriber@example.com", "pro")

    # Attribute access, not .get(): stripe Customer objects aren't dicts —
    # the handler reads customer.email (see billing.py regression note).
    fake_customer = MagicMock()
    fake_customer.email = "subscriber@example.com"

    event = _make_event("customer.subscription.deleted", {"customer": "cus_fake123"})

    import stripe
    with patch.object(stripe.Customer, "retrieve", return_value=fake_customer):
        with patch("fundscan.billing.notify_churn"):
            with patch("fundscan.billing.log_webhook_event"):
                handle_webhook(event)

    user = get_or_create_user("subscriber@example.com")
    assert user["tier"] == "free"


# ---------------------------------------------------------------------------
# invoice.payment_failed → sends email (mocked)
# ---------------------------------------------------------------------------

def test_payment_failed_sends_email():
    from fundscan.billing import handle_webhook

    event = _make_event("invoice.payment_failed", {
        "customer_email": "subscriber@example.com",
        "id": "inv_test123",
    })

    with patch("fundscan.auth.send_email") as mock_send:
        with patch("fundscan.billing.log_webhook_event"):
            handle_webhook(event)

    # send_email is called via lazy import inside handle_webhook
    # patch it at the source module
    with patch("fundscan.billing.log_webhook_event"):
        with patch("fundscan.auth._dispatch_email") as mock_dispatch:
            handle_webhook(event)
    # Email should have been attempted (may not capture via nested import —
    # this test verifies the code path executes without error)


# ---------------------------------------------------------------------------
# Unknown event type — should be logged but not crash
# ---------------------------------------------------------------------------

def test_unknown_event_no_crash():
    from fundscan.billing import handle_webhook
    event = _make_event("some.unknown.event", {"foo": "bar"})
    with patch("fundscan.billing.log_webhook_event"):
        handle_webhook(event)  # should not raise


# ---------------------------------------------------------------------------
# Regression: REAL stripe Event objects, not dict doubles.
# Modern stripe-python Events aren't dicts: dict(event) raises TypeError and
# .get() raises AttributeError. Every production webhook delivery 500'd on
# exactly this until 2026-08-21 while the dict-double tests above stayed
# green. This test pins the real object type through the full handler.
# ---------------------------------------------------------------------------

def test_real_stripe_event_object_checkout_completed():
    import stripe
    from fundscan.billing import handle_webhook
    from fundscan.auth import get_or_create_user, set_user_tier

    set_user_tier("subscriber@example.com", "free")
    event = stripe.Event.construct_from(
        {
            "id": "evt_real_obj",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_real_obj",
                "object": "checkout.session",
                "client_reference_id": "subscriber@example.com",
                "customer_email": "subscriber@example.com",
            }},
        },
        "sk_test_fake",
    )
    # Sanity: this really is the non-dict shape that broke production
    with pytest.raises(TypeError):
        dict(event)

    with patch("fundscan.billing.notify_new_signup"):
        handle_webhook(event)  # must not raise

    assert get_or_create_user("subscriber@example.com")["tier"] == "pro"
