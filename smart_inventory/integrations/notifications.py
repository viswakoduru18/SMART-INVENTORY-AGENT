"""Retailer / procurement / supplier messaging (WhatsApp Business via Gupshup, AiSensy or any webhook).

If no webhook is configured, messages are stored with status LOGGED so the
console still shows exactly what would have been sent (shadow-safe).
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Notification

log = logging.getLogger(__name__)

TEMPLATES = {
    "sourced_eta": "Hi {retailer}, {sku} is available on request. Expected dispatch in about {eta} hours. Your order is on hold for you. - Acintyo",
    "not_available": "Hi {retailer}, {sku} is currently not available from any authorised source ({reason}). Reply CALLBACK and we will notify you the moment it is in stock. - Acintyo",
    "back_in_stock": "Hi {retailer}, {sku} is back in stock. Reply ORDER {qty} to place your order. - Acintyo",
    "special_offer_lot": "Special lot for {retailer}: {sku} at {discount}% discount (Special / Non-Returnable terms). Normal terms remain available at {normal_discount}%. Valid till {valid_to}.",
    "supplier_inquiry": "Namaste {supplier}, please share availability, batch, expiry, rate and scheme for: {sku} ({composition}), qty {qty}. Required for {warehouse}. - Acintyo Procurement",
    "procurement_briefing": "{body}",
}


def render(template: str, **kwargs: object) -> str:
    return TEMPLATES[template].format(**kwargs)


def send(db: Session, channel: str, recipient: str | None, template: str, ref: str | None = None, **kwargs: object) -> Notification:
    settings = get_settings()
    body = render(template, **kwargs)
    note = Notification(channel=channel, recipient=recipient or "unknown", template=template, body=body, ref=ref)
    if channel == "whatsapp" and settings.whatsapp_webhook_url and recipient:
        try:
            resp = httpx.post(
                settings.whatsapp_webhook_url,
                json={"to": recipient, "template": template, "body": body, "ref": ref},
                headers={"Authorization": f"Bearer {settings.whatsapp_api_key or ''}"},
                timeout=10,
            )
            resp.raise_for_status()
            note.status = "SENT"
        except httpx.HTTPError as exc:
            log.warning("whatsapp send failed: %s", exc)
            note.status = "FAILED"
    else:
        note.status = "LOGGED"
    db.add(note)
    return note
