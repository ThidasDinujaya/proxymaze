from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx

from models import AlertObject, IntegrationRequest, IntegrationResponse

logger = logging.getLogger(__name__)

TRANSIENT_ERRORS = {500, 502, 503, 504}
MAX_RETRIES = 20
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 30.0


def _utcnow_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _slack_fired(alert: AlertObject, username: str) -> Dict[str, Any]:
    return {
        "username": username,
        "text": f":red_circle: Alert fired — failure rate {alert.failure_rate:.0%} exceeds threshold",
        "attachments": [
            {
                "color": "#FF0000",
                "fields": [
                    {"title": "Alert ID", "value": alert.alert_id, "short": True},
                    {"title": "Failure Rate", "value": f"{alert.failure_rate:.2%}", "short": True},
                    {"title": "Failed Proxies", "value": str(alert.failed_proxies), "short": True},
                    {"title": "Threshold", "value": f"{alert.threshold:.2%}", "short": True},
                    {"title": "Failed IDs", "value": ", ".join(alert.failed_proxy_ids) or "—", "short": False},
                    {"title": "Fired At", "value": alert.fired_at, "short": True},
                ],
                "footer": "ProxyMaze • Torch Labs",
                "ts": _utcnow_ts(),
            }
        ],
    }


def _slack_resolved(alert: AlertObject, username: str) -> Dict[str, Any]:
    return {
        "username": username,
        "text": ":large_green_circle: Alert resolved — pool has recovered.",
        "attachments": [
            {
                "color": "#36A64F",
                "fields": [
                    {"title": "Alert ID", "value": alert.alert_id, "short": True},
                    {"title": "Failure Rate", "value": f"{alert.failure_rate:.2%}", "short": True},
                    {"title": "Failed Proxies", "value": str(alert.failed_proxies), "short": True},
                    {"title": "Threshold", "value": f"{alert.threshold:.2%}", "short": True},
                    {"title": "Failed IDs", "value": ", ".join(alert.failed_proxy_ids) or "—", "short": False},
                    {"title": "Fired At", "value": alert.fired_at, "short": True},
                ],
                "footer": "ProxyMaze • Torch Labs",
                "ts": _utcnow_ts(),
            }
        ],
    }


def _discord_fired(alert: AlertObject, username: str) -> Dict[str, Any]:
    return {
        "username": username,
        "embeds": [
            {
                "title": "🔴 Proxy Pool Alert Fired",
                "description": (
                    f"Failure rate has exceeded the threshold.\n"
                    f"**Rate:** {alert.failure_rate:.2%} | "
                    f"**Threshold:** {alert.threshold:.2%}"
                ),
                "color": 16711680,
                "fields": [
                    {"name": "Alert ID", "value": alert.alert_id, "inline": True},
                    {"name": "Failure Rate", "value": f"{alert.failure_rate:.2%}", "inline": True},
                    {"name": "Failed Proxies", "value": str(alert.failed_proxies), "inline": True},
                    {"name": "Threshold", "value": f"{alert.threshold:.2%}", "inline": True},
                    {"name": "Failed IDs", "value": ", ".join(alert.failed_proxy_ids) or "—", "inline": False},
                ],
                "footer": {"text": "ProxyMaze • Torch Labs"},
            }
        ],
    }


def _discord_resolved(alert: AlertObject, username: str) -> Dict[str, Any]:
    return {
        "username": username,
        "embeds": [
            {
                "title": "🟢 Proxy Pool Alert Resolved",
                "description": "The proxy pool has recovered below the threshold.",
                "color": 3581519,
                "fields": [
                    {"name": "Alert ID", "value": alert.alert_id, "inline": True},
                    {"name": "Failure Rate", "value": f"{alert.failure_rate:.2%}", "inline": True},
                    {"name": "Failed Proxies", "value": str(alert.failed_proxies), "inline": True},
                    {"name": "Threshold", "value": f"{alert.threshold:.2%}", "inline": True},
                    {"name": "Failed IDs", "value": ", ".join(alert.failed_proxy_ids) or "—", "inline": False},
                ],
                "footer": {"text": "ProxyMaze • Torch Labs"},
            }
        ],
    }


class IntegrationEntry:
    def __init__(self, integration_id: str, req: IntegrationRequest) -> None:
        self.integration_id = integration_id
        self.type = req.type
        self.webhook_url = req.webhook_url
        self.username = req.username
        self.events = set(req.events)


class IntegrationManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._integrations: Dict[str, IntegrationEntry] = {}

    async def register(self, req: IntegrationRequest) -> IntegrationResponse:
        async with self._lock:
            iid = "int-" + uuid.uuid4().hex[:8]
            entry = IntegrationEntry(iid, req)
            self._integrations[iid] = entry
        return IntegrationResponse(
            integration_id=iid,
            type=req.type,
            webhook_url=req.webhook_url,
            username=req.username,
            events=req.events,
        )

    async def dispatch_fired(self, alert: AlertObject) -> None:
        async with self._lock:
            entries = list(self._integrations.values())
        for entry in entries:
            if "alert.fired" not in entry.events:
                continue
            if entry.type == "slack":
                payload = _slack_fired(alert, entry.username)
            elif entry.type == "discord":
                payload = _discord_fired(alert, entry.username)
            else:
                continue
            asyncio.create_task(
                _deliver_with_retry(entry.webhook_url, payload)
            )

    async def dispatch_resolved(self, alert: AlertObject) -> None:
        async with self._lock:
            entries = list(self._integrations.values())
        for entry in entries:
            if "alert.resolved" not in entry.events:
                continue
            if entry.type == "slack":
                payload = _slack_resolved(alert, entry.username)
            elif entry.type == "discord":
                payload = _discord_resolved(alert, entry.username)
            else:
                continue
            asyncio.create_task(
                _deliver_with_retry(entry.webhook_url, payload)
            )


async def _deliver_with_retry(url: str, payload: Dict[str, Any]) -> None:
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
                logger.info("Integration delivered to %s", url)
                return
        except Exception as exc:
            logger.warning("Integration error to %s: %s", url, exc)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)