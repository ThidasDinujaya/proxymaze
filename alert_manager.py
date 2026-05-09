from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from models import AlertObject

logger = logging.getLogger(__name__)

THRESHOLD = 0.20


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_alert_id() -> str:
    return "alert-" + uuid.uuid4().hex[:6]


class AlertManager:
    def __init__(self, on_fired, on_resolved) -> None:
        self._lock = asyncio.Lock()
        self._alerts: List[AlertObject] = []
        self._active_alert: Optional[AlertObject] = None
        self._on_fired = on_fired
        self._on_resolved = on_resolved

    async def evaluate(
        self,
        total: int,
        up: int,
        down: int,
        failed_ids: List[str],
    ) -> None:
        if total == 0:
            return

        failure_rate = round(down / total, 4)
        breach = failure_rate >= THRESHOLD

        async with self._lock:
            if breach and self._active_alert is None:
                alert = AlertObject(
                    alert_id=_new_alert_id(),
                    status="active",
                    failure_rate=failure_rate,
                    total_proxies=total,
                    failed_proxies=down,
                    failed_proxy_ids=list(failed_ids),
                    threshold=THRESHOLD,
                    fired_at=_utcnow(),
                    resolved_at=None,
                    message="Proxy pool failure rate exceeded threshold",
                )
                self._active_alert = alert
                self._alerts.append(alert)
                logger.info(
                    "Alert fired: %s (rate=%.2f)",
                    alert.alert_id,
                    failure_rate,
                )
                asyncio.create_task(self._on_fired(alert))

            elif not breach and self._active_alert is not None:
                self._active_alert.status = "resolved"
                self._active_alert.resolved_at = _utcnow()
                resolved = self._active_alert
                self._active_alert = None
                logger.info("Alert resolved: %s", resolved.alert_id)
                asyncio.create_task(self._on_resolved(resolved))

    async def get_all_alerts(self) -> List[AlertObject]:
        async with self._lock:
            return list(self._alerts)

    async def active_alert_count(self) -> int:
        async with self._lock:
            return 1 if self._active_alert is not None else 0

    async def total_alert_count(self) -> int:
        async with self._lock:
            return len(self._alerts)