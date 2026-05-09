from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

TRANSIENT_ERRORS = {500, 502, 503, 504}
MAX_RETRIES = 20
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 30.0


class WebhookReceiver:
    def __init__(self, webhook_id: str, url: str) -> None:
        self.webhook_id = webhook_id
        self.url = url


class WebhookManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._receivers: Dict[str, WebhookReceiver] = {}
        self._delivery_count: int = 0

    async def register(self, url: str) -> WebhookReceiver:
        async with self._lock:
            wid = "wh-" + uuid.uuid4().hex[:8]
            receiver = WebhookReceiver(wid, url)
            self._receivers[wid] = receiver
            return receiver

    async def broadcast(self, payload: Dict[str, Any]) -> None:
        async with self._lock:
            receivers = list(self._receivers.values())
        for receiver in receivers:
            asyncio.create_task(
                self._deliver_with_retry(receiver.url, payload),
                name=f"webhook-{receiver.webhook_id}",
            )

    async def _deliver_with_retry(
        self, url: str, payload: Dict[str, Any]
    ) -> None:
        backoff = INITIAL_BACKOFF
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        url,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    )
                if resp.status_code not in TRANSIENT_ERRORS:
                    async with self._lock:
                        self._delivery_count += 1
                    logger.info(
                        "Webhook delivered to %s (status=%d)",
                        url,
                        resp.status_code,
                    )
                    return
            except Exception as exc:
                logger.warning("Webhook error to %s: %s", url, exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def delivery_count(self) -> int:
        async with self._lock:
            return self._delivery_count