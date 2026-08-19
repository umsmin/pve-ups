"""Optional notifications: generic webhook.

Notifications are best-effort: a failure to notify must never affect the shutdown
logic, so every send is wrapped and only logged on error.

The payload shape is selectable (see config.WebhookFormat). Every format is one entry in
FORMATTERS, rendering a notification into keyword arguments for ``httpx.post`` — a new
target system is a table row, not new plumbing. Message texts are English only.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

import httpx

from . import __version__, db
from .config import Notifications, WebhookConfig, WebhookFormat

log = logging.getLogger("pve-usv.notify")

# Severity ranking for the "send from this level upwards" filter.
_RANK = {db.INFO: 0, db.WARNING: 1, db.CRITICAL: 2}

# Adaptive card colours per severity (Teams renders these as green/amber/red).
_CARD_COLOR = {db.INFO: "Good", db.WARNING: "Warning", db.CRITICAL: "Attention"}


def _value(setting) -> str:
    """Enum member or bare string — plain attribute assignment skips pydantic validation."""
    return getattr(setting, "value", setting)


def _rank(severity: str) -> int:
    """Rank of a severity; an unknown one counts as a warning so it is never dropped."""
    return _RANK.get(severity, _RANK[db.WARNING])


def _passes(hook: WebhookConfig, severity: str) -> bool:
    return _rank(severity) >= _rank(_value(hook.min_severity))


# --- payload rendering ------------------------------------------------------
def _facts(payload: dict) -> list[tuple[str, str]]:
    """Compact key/value summary of the status snapshot, shared by teams and text.

    Reads defensively throughout: the snapshot is the engine's business, and a changed
    key must degrade the notification, never break it.
    """
    facts: list[tuple[str, str]] = []
    try:
        ups_lines = []
        for u in payload.get("ups") or []:
            bits = []
            if not u.get("reachable"):
                bits.append("unreachable")
            else:
                bits.append(str(u.get("power_source") or "?"))
                if u.get("battery_charge_pct") is not None:
                    bits.append(f"{u['battery_charge_pct']} %")
                if u.get("runtime_remaining_min") is not None:
                    bits.append(f"{u['runtime_remaining_min']} min")
            ups_lines.append(f"{u.get('name') or u.get('id') or 'UPS'}: {', '.join(bits)}")
        if ups_lines:
            facts.append(("UPS", " · ".join(ups_lines)))

        host_lines = []
        for h in payload.get("hosts") or []:
            state = h.get("shutdown_state") or ("eligible" if h.get("eligible") else "waiting")
            host_lines.append(f"{h.get('name') or '?'}: {state}")
        if host_lines:
            facts.append(("Hosts", " · ".join(host_lines)))

        app = payload.get("appliance") or {}
        facts.append(("Mode", "DRY-RUN" if app.get("dry_run") else "ARMED"))
        facts.append(("Time", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")))
        facts.append(("Appliance", f"PVE-UPS {app.get('version') or __version__}"))
    except Exception as exc:  # noqa: BLE001 - a malformed snapshot must not stop the send
        log.warning("Could not summarise the status snapshot: %s", exc)
    return facts


def _render_json(subject: str, body: str, severity: str, payload: dict) -> dict:
    """The original payload: subject, body and the complete status snapshot."""
    return {"json": {"subject": subject, "body": body, "severity": severity, "status": payload}}


def _render_teams(subject: str, body: str, severity: str, payload: dict) -> dict:
    """Microsoft Teams adaptive card, wrapped in the incoming-webhook message envelope."""
    card_body: list[dict] = [
        {
            "type": "TextBlock",
            "text": subject,
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
            "color": _CARD_COLOR.get(severity, "Default"),
        }
    ]
    if body:
        card_body.append({"type": "TextBlock", "text": body, "wrap": True})
    facts = _facts(payload)
    if facts:
        card_body.append(
            {"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in facts]}
        )
    return {
        "json": {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": card_body,
                    },
                }
            ],
        }
    }


def _render_text(subject: str, body: str, severity: str, payload: dict) -> dict:
    """Human-readable short status as text/plain."""
    lines = [f"[{severity.upper()}] {subject}"]
    if body:
        lines.append(body)
    lines.extend(f"{k}: {v}" for k, v in _facts(payload))
    return {
        "content": "\n".join(lines).encode("utf-8"),
        "headers": {"Content-Type": "text/plain; charset=utf-8"},
    }


# The extension point: one entry per selectable format (see config.WebhookFormat).
FORMATTERS: dict[str, Callable[[str, str, str, dict], dict]] = {
    WebhookFormat.json.value: _render_json,
    WebhookFormat.teams.value: _render_teams,
    WebhookFormat.text.value: _render_text,
}


# --- sending ----------------------------------------------------------------
async def send_webhook(
    hook: WebhookConfig, subject: str, body: str, severity: str, payload: dict
) -> str:
    """POST one notification and return a short result. Raises on any failure.

    Deliberately ignores ``enabled`` and ``min_severity``: the filter belongs to the
    event path in :func:`notify`, so the UI's test button can send unconditionally.
    """
    render = FORMATTERS.get(_value(hook.format), _render_json)
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(hook.url, **render(subject, body, severity, payload))
    resp.raise_for_status()
    return f"HTTP {resp.status_code}"


async def notify(
    notifications: Notifications,
    subject: str,
    body: str,
    payload: dict,
    severity: str = db.INFO,
) -> None:
    """Fire the webhook notification, swallowing all errors.

    Everything is inside the guard, filter included: this runs on the engine's poll loop,
    which must never be brought down by a notification.
    """
    try:
        hook = notifications.webhook
        if not (hook.enabled and hook.url) or not _passes(hook, severity):
            return
        await send_webhook(hook, subject, body, severity, payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("Webhook notification failed: %s", exc)
