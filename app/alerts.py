"""Alerts: always logged, optionally sent to Telegram."""

from __future__ import annotations

import logging
from enum import Enum

import httpx

from app.config import Settings

logger = logging.getLogger("alerts")


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


async def notify(settings: Settings, level: AlertLevel, text: str) -> None:
    message = f"[{level.value}] {text}"
    if level == AlertLevel.INFO:
        logger.info(message)
    else:
        logger.warning(message)

    token = settings.alert_telegram_bot_token
    chat_id = settings.alert_telegram_chat_id
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={"chat_id": chat_id, "text": message})
        if resp.status_code >= 400:
            logger.warning("telegram alert rejected: status=%s", resp.status_code)
    except Exception as exc:  # alerts must never break the job
        logger.warning("telegram alert failed: %s", type(exc).__name__)
