from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

import config as cfg
from models import CheckRecord, ProxyDetail, ProxySummary, PoolResponse

logger = logging.getLogger(__name__)


class ProxyState:
    __slots__ = (
        "proxy_id", "url", "status",
        "last_checked_at", "consecutive_failures",
        "total_checks", "up_checks", "history",
    )

    def __init__(self, proxy_id: str, url: str) -> None:
        self.proxy_id = proxy_id
        self.url = url
        self.status: str = "pending"
        self.last_checked_at: Optional[str] = None
        self.consecutive_failures: int = 0
        self.total_checks: int = 0
        self.up_checks: int = 0
        self.history: List[CheckRecord] = []

    def to_summary(self) -> ProxySummary:
        return ProxySummary(
            id=self.proxy_id,
            url=self.url,
            status=self.status,
            last_checked_at=self.last_checked_at,
            consecutive_failures=self.consecutive_failures,
        )

    def to_detail(self) -> ProxyDetail:
        uptime = (
            round(self.up_checks / self.total_checks * 100, 1)
            if self.total_checks > 0
            else 0.0
        )
        return ProxyDetail(
            id=self.proxy_id,
            url=self.url,
            status=self.status,
            last_checked_at=self.last_checked_at,
            consecutive_failures=self.consecutive_failures,
            total_checks=self.total_checks,
            uptime_percentage=uptime,
            history=list(self.history),
        )

    def record_check(self, status: str, checked_at: str) -> None:
        self.status = status
        self.last_checked_at = checked_at
        self.total_checks += 1
        if status == "up":
            self.up_checks += 1
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
        self.history.append(CheckRecord(checked_at=checked_at, status=status))
        if len(self.history) > 1000:
            self.history = self.history[-1000:]


class ProxyPool:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._proxies: Dict[str, ProxyState] = {}
        self._total_checks: int = 0

    async def add_proxies(self, urls: List[str], replace: bool) -> List[ProxySummary]:
        async with self._lock:
            if replace:
                self._proxies.clear()
            added: List[ProxySummary] = []
            for url in urls:
                pid = _extract_id(url)
                if pid not in self._proxies:
                    state = ProxyState(pid, url)
                    self._proxies[pid] = state
                added.append(self._proxies[pid].to_summary())
            return added

    async def clear(self) -> None:
        async with self._lock:
            self._proxies.clear()

    async def get_pool_response(self) -> PoolResponse:
        async with self._lock:
            states = list(self._proxies.values())
        total = len(states)
        up = sum(1 for s in states if s.status == "up")
        down = sum(1 for s in states if s.status == "down")
        failure_rate = round(down / total, 4) if total > 0 else 0.0
        proxies = [s.to_summary() for s in states]
        return PoolResponse(
            total=total,
            up=up,
            down=down,
            failure_rate=failure_rate,
            proxies=proxies,
        )

    async def get_proxy(self, proxy_id: str) -> Optional[ProxyDetail]:
        async with self._lock:
            state = self._proxies.get(proxy_id)
            return state.to_detail() if state else None

    async def get_history(self, proxy_id: str) -> Optional[List[CheckRecord]]:
        async with self._lock:
            state = self._proxies.get(proxy_id)
            return list(state.history) if state else None

    async def pool_size(self) -> int:
        async with self._lock:
            return len(self._proxies)

    async def total_checks_count(self) -> int:
        async with self._lock:
            return self._total_checks

    async def run_check_cycle(self) -> Tuple[int, int, int, List[str]]:
        conf = cfg.get_config()
        timeout_s = conf["request_timeout_ms"] / 1000.0

        async with self._lock:
            states = list(self._proxies.values())

        if not states:
            return 0, 0, 0, []

        results = await asyncio.gather(
            *[_probe(state.url, timeout_s) for state in states],
            return_exceptions=True,
        )

        now = _utcnow()
        async with self._lock:
            for state, result in zip(states, results):
                if state.proxy_id not in self._proxies:
                    continue
                status = result if isinstance(result, str) else "down"
                self._proxies[state.proxy_id].record_check(status, now)
                self._total_checks += 1

            live_states = list(self._proxies.values())

        total = len(live_states)
        up_count = sum(1 for s in live_states if s.status == "up")
        down_count = sum(1 for s in live_states if s.status == "down")
        failed_ids = [s.proxy_id for s in live_states if s.status == "down"]
        return total, up_count, down_count, failed_ids


async def _probe(url: str, timeout_s: float) -> str:
    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as client:
            resp = await client.get(url)
            return "up" if 200 <= resp.status_code < 300 else "down"
    except Exception:
        return "down"


def _extract_id(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MonitorLoop:
    def __init__(self, pool: ProxyPool, on_cycle_done) -> None:
        self._pool = pool
        self._on_cycle_done = on_cycle_done
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._wake_event.clear()
            self._task = asyncio.create_task(self._loop(), name="monitor-loop")

    def wake(self) -> None:
        self._wake_event.set()

    async def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _sleep_or_wake(self, timeout: float) -> None:
        stop_task = asyncio.create_task(self._stop_event.wait())
        wake_task = asyncio.create_task(self._wake_event.wait())
        try:
            await asyncio.wait(
                {stop_task, wake_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (stop_task, wake_task):
                if not task.done():
                    task.cancel()
            self._wake_event.clear()

    async def _loop(self) -> None:
        logger.info("Monitor loop started.")
        while not self._stop_event.is_set():
            interval = cfg.get_config()["check_interval_seconds"]
            try:
                total, up, down, failed_ids = await self._pool.run_check_cycle()
                await self._on_cycle_done(total, up, down, failed_ids)
            except Exception:
                logger.exception("Error in monitor loop.")
            await self._sleep_or_wake(float(interval))
        logger.info("Monitor loop stopped.")