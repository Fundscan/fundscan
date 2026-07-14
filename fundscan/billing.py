"""
Stripe billing integration.

CONFIG REQUIRED (set in .env):
  STRIPE_SECRET_KEY     — from Stripe dashboard → Developers → API keys
  STRIPE_PRICE_ID       — the price ID for the £20/month Pro plan (price_xxx)
  STRIPE_WEBHOOK_SECRET — from Stripe dashboard → Webhooks → signing secret (whsec_xxx)

Webhook events handled:
  checkout.session.completed     → set user tier = pro
  customer.subscription.deleted  → set user tier = free
  invoice.payment_failed         → log warning
"""
import json
import logging
import os
from datetime import datetime, timezone

import stripe

from .auth import set_user_tier
from .db import get_conn
from .alerts import notify_new_signup, notify_churn

log = logging.getLogger(__name__)

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")


def portal_url(email: str, return_url: str) -> str:
    """
    Create a Stripe Billing Portal session for an existing customer.
    Returns the portal URL so the user can manage/cancel their subscription.
    The customer is looked up by email — requires STRIPE_SECRET_KEY.
    """
    stripe.api_key = STRIPE_SECRET_KEY
    # Find the customer by email
    customers = stripe.Customer.list(email=email, limit=1)
    if not customers.data:
        raise ValueError(f"No Stripe customer found for {email}")
    customer_id = customers.data[0].id
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )
    return session.url


def checkout_url(email: str) -> str:
    """
    Create a Stripe Checkout Session for a £20/month subscription.
    Returns the hosted checkout URL.
    """
    stripe.api_key = STRIPE_SECRET_KEY
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=email,
        client_reference_id=email,   # echoed back in webhook so we can find the user
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=f"{os.getenv('BASE_URL', 'http://localhost:8000')}/account?upgraded=1",
        cancel_url=f"{os.getenv('BASE_URL', 'http://localhost:8000')}/billing/checkout",
    )
    return session.url


def verify_webhook(body: bytes, sig_header: str) -> stripe.Event:
    """
    Verify Stripe webhook signature and return the Event object.
    Raises stripe.error.SignatureVerificationError on failure.
    """
    stripe.api_key = STRIPE_SECRET_KEY
    return stripe.Webhook.construct_event(body, sig_header, STRIPE_WEBHOOK_SECRET)


def log_webhook_event(event_type: str, payload: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO webhook_events (received_at, event_type, payload) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), event_type, json.dumps(payload)),
        )


def handle_webhook(event: stripe.Event) -> None:
    """Process a verified Stripe webhook event."""
    event_type = event["type"]
    log_webhook_event(event_type, dict(event))

    if event_type == "checkout.session.completed":
        obj = event["data"]["object"]
        # client_reference_id is the email we set at checkout creation
        email = obj.get("client_reference_id") or obj.get("customer_email")
        if email:
            set_user_tier(email, "pro")
            notify_new_signup(email)
            log.info("checkout.session.completed → %s tier=pro", email)
        else:
            log.warning("checkout.session.completed missing email, event id=%s", event["id"])

    elif event_type == "customer.subscription.deleted":
        # Subscription cancelled/expired — downgrade user
        customer_id = event["data"]["object"].get("customer")
        if customer_id:
            try:
                stripe.api_key = STRIPE_SECRET_KEY
                customer = stripe.Customer.retrieve(customer_id)
                email = customer.get("email")
                if email:
                    set_user_tier(email, "free")
                    notify_churn(email)
                    log.info("subscription.deleted → %s tier=free", email)
            except Exception as e:
                log.error("Failed to retrieve customer %s: %s", customer_id, e)

    elif event_type == "invoice.payment_failed":
        obj = event["data"]["object"]
        email = obj.get("customer_email")
        log.warning("Payment failed for %s (invoice %s)", email, obj.get("id"))
        if email:
            from .auth import send_email
            send_email(
                email,
                "FundScan — payment failed",
                (
                    "Hi,\n\nWe couldn't process your FundScan subscription payment.\n\n"
                    "Please update your payment method to keep Pro access:\n"
                    f"{os.getenv('BASE_URL', 'https://fundscan.uk')}/account\n\n"
                    "Your account will stay active until the next retry.\n\n"
                    "— FundScan"
                ),
            )
