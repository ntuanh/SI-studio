"""Outbound notifications — currently Telegram.

Used by `services/autorun.py` to report an unattended schedule's progress to a
phone. Two rules shape everything here:

**A notifier must never break the thing it reports on.** Every public call
swallows its own failures and returns a bool. A wrong chat id, an expired bot
token or a lab with no internet degrades to "no messages", never to a crashed
run — which would be the worst possible failure mode for a feature whose entire
job is telling you a run crashed.

**No new dependencies.** Notifications are a handful of small POSTs per run, so
`urllib` on a worker thread is enough and keeps `requirements.txt` (and every
deploy that pins it) untouched. `asyncio.to_thread` keeps the event loop free
while the socket blocks.

The bot token is a credential: it appears in the request *URL*, so it is
scrubbed from every log line and error string by `_redact`.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"

#: Telegram rejects `text` longer than this (4096 chars). We trim below the
#: limit and mark the cut rather than letting the send fail outright.
MAX_TEXT = 3900


def esc(value: Any) -> str:
    """Escape for Telegram's HTML parse mode.

    Only `&`, `<` and `>` are special, which is why HTML is used here in
    preference to Markdown: script output is full of `_`, `*` and `[`, and any
    unbalanced one of those makes Telegram reject the whole message.
    """
    return html.escape(str(value), quote=False)


def _redact(text: str) -> str:
    token = (settings.telegram_bot_token or "").strip()
    return text.replace(token, "***") if token else text


def _clip(text: str) -> str:
    if len(text) <= MAX_TEXT:
        return text
    return text[:MAX_TEXT] + "\n… (truncated)"


class TelegramNotifier:
    """Thin Telegram Bot API client.

    Configure with `TELEGRAM_BOT_TOKEN` (from @BotFather) and
    `TELEGRAM_CHAT_ID`. `enabled` is False when either is missing, so the
    autorun code can call `send()` unconditionally.
    """

    def __init__(self) -> None:
        self._last_error: str = ""

    # ------------------------------------------------------------------ state
    @property
    def token(self) -> str:
        return (settings.telegram_bot_token or "").strip()

    @property
    def chat_id(self) -> str:
        return str(settings.telegram_chat_id or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    @property
    def enabled(self) -> bool:
        return bool(settings.notify_enabled and self.configured)

    @property
    def last_error(self) -> str:
        return self._last_error

    def status(self) -> dict[str, Any]:
        """Safe to expose over the API — reports *whether* it is configured,
        never the token itself."""
        return {
            "channel": "telegram",
            "enabled": self.enabled,
            "configured": self.configured,
            "notify_enabled": settings.notify_enabled,
            "has_token": bool(self.token),
            "has_chat_id": bool(self.chat_id),
            "chat_id": self.chat_id if self.chat_id else "",
            "last_error": self._last_error,
        }

    # ----------------------------------------------------------------- sending
    async def send(self, text: str, *, silent: bool = False) -> bool:
        """Post one message. Returns True on success; never raises.

        `silent` delivers without a notification sound — used for routine
        progress so only the outcomes that matter buzz the phone.
        """
        if not settings.notify_enabled:
            return False
        if not self.configured:
            self._last_error = "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"
            log.debug("notify skipped: %s", self._last_error)
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": _clip(text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
        try:
            await asyncio.to_thread(self._post, "sendMessage", payload)
        except Exception as exc:  # noqa: BLE001 - a notifier must not propagate
            self._last_error = _redact(str(exc))
            log.warning("telegram send failed: %s", self._last_error)
            return False

        self._last_error = ""
        return True

    async def verify(self) -> dict[str, Any]:
        """`getMe` + a test message — powers `POST /autorun/notify/test`."""
        if not self.configured:
            return {"ok": False, "error": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"}
        try:
            me = await asyncio.to_thread(self._post, "getMe", {})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": _redact(str(exc))}

        bot = (me.get("result") or {}).get("username", "")
        sent = await self.send(
            "✅ <b>SI-studio notifications are working.</b>\n"
            "Auto-run will report progress and failures here.",
        )
        return {
            "ok": sent,
            "bot": bot,
            "chat_id": self.chat_id,
            "error": "" if sent else (self._last_error or "message rejected"),
        }

    # ------------------------------------------------------------------ http
    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Blocking POST — always called through `asyncio.to_thread`."""
        url = f"{API_ROOT}/bot{self.token}/{method}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=settings.notify_timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            # Telegram explains the real problem in the body ("chat not found",
            # "Unauthorized"), which is far more useful than the status line.
            detail = ""
            try:
                detail = (
                    json.loads(exc.read().decode("utf-8", "replace")).get("description") or ""
                )
            except Exception:  # noqa: BLE001 - fall back to the status line
                pass
            raise RuntimeError(f"telegram {method}: {exc.code} {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"telegram {method}: unreachable ({exc.reason})") from exc

        if not body.get("ok"):
            raise RuntimeError(f"telegram {method}: {body.get('description', 'rejected')}")
        return body


#: Process-wide singleton, mirroring `services.metrics_bus.bus`.
notifier = TelegramNotifier()
