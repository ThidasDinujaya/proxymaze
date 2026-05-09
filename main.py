from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

import config as cfg
from alert_manager import AlertManager
from integration_manager import IntegrationManager
from models import (
    AlertObject,
    ConfigRequest,
    IntegrationRequest,
    IntegrationResponse,
    MetricsResponse,
    PoolResponse,
    ProxyDetail,
    WebhookRegisterResponse,
)
from proxy_monitor import MonitorLoop, ProxyPool
from webhook_manager import WebhookManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger("proxymaze.main")

pool = ProxyPool()
webhook_mgr = WebhookManager()
integration_mgr = IntegrationManager()


async def _on_alert_fired(alert: AlertObject) -> None:
    payload: Dict[str, Any] = {
        "event": "alert.fired",
        "alert_id": alert.alert_id,
        "fired_at": alert.fired_at,
        "failure_rate": alert.failure_rate,
        "total_proxies": alert.total_proxies,
        "failed_proxies": alert.failed_proxies,
        "failed_proxy_ids": alert.failed_proxy_ids,
        "threshold": alert.threshold,
        "message": alert.message,
    }
    await webhook_mgr.broadcast(payload)
    await integration_mgr.dispatch_fired(alert)


async def _on_alert_resolved(alert: AlertObject) -> None:
    payload: Dict[str, Any] = {
        "event": "alert.resolved",
        "alert_id": alert.alert_id,
        "resolved_at": alert.resolved_at,
    }
    await webhook_mgr.broadcast(payload)
    await integration_mgr.dispatch_resolved(alert)


alert_mgr = AlertManager(
    on_fired=_on_alert_fired,
    on_resolved=_on_alert_resolved,
)


async def _on_cycle_done(
    total: int, up: int, down: int, failed_ids: List[str]
) -> None:
    await alert_mgr.evaluate(total, up, down, failed_ids)


monitor = MonitorLoop(pool=pool, on_cycle_done=_on_cycle_done)


@asynccontextmanager
async def lifespan(app: FastAPI):
    monitor.start()
    logger.info("ProxyMaze started.")
    yield
    await monitor.stop()
    logger.info("ProxyMaze stopped.")


app = FastAPI(title="ProxyMaze'26", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/config", status_code=200)
async def set_config(body: ConfigRequest):
    cfg.set_config(
        check_interval_seconds=body.check_interval_seconds,
        request_timeout_ms=body.request_timeout_ms,
    )
    return cfg.get_config()


@app.get("/config")
async def get_config():
    return cfg.get_config()


@app.post("/proxies", status_code=201)
async def add_proxies(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed JSON.")

    urls = body.get("proxies", [])
    replace = body.get("replace", False)

    if not isinstance(urls, list):
        raise HTTPException(status_code=400, detail="'proxies' must be a list.")

    added = await pool.add_proxies(urls, replace)
    return {
        "accepted": len(added),
        "proxies": [p.model_dump() for p in added],
    }


@app.get("/proxies")
async def list_proxies():
    pr: PoolResponse = await pool.get_pool_response()
    return pr.model_dump()


@app.get("/proxies/{proxy_id}")
async def get_proxy(proxy_id: str):
    detail: ProxyDetail | None = await pool.get_proxy(proxy_id)
    if detail is None:
        raise HTTPException(
            status_code=404, detail=f"Proxy '{proxy_id}' not found."
        )
    return detail.model_dump()


@app.get("/proxies/{proxy_id}/history")
async def get_proxy_history(proxy_id: str):
    history = await pool.get_history(proxy_id)
    if history is None:
        raise HTTPException(
            status_code=404, detail=f"Proxy '{proxy_id}' not found."
        )
    return [r.model_dump() for r in history]


@app.delete("/proxies", status_code=204)
async def clear_proxies():
    await pool.clear()


@app.get("/alerts")
async def list_alerts():
    alerts = await alert_mgr.get_all_alerts()
    return [a.model_dump() for a in alerts]


@app.post("/webhooks", status_code=201)
async def register_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed JSON.")

    url = body.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="'url' is required.")

    receiver = await webhook_mgr.register(url)
    return WebhookRegisterResponse(
        webhook_id=receiver.webhook_id,
        url=receiver.url,
    ).model_dump()


@app.post("/integrations", status_code=201)
async def register_integration(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed JSON.")

    itype = body.get("type", "").lower()
    if itype not in ("slack", "discord"):
        raise HTTPException(
            status_code=400, detail="'type' must be 'slack' or 'discord'."
        )

    webhook_url = body.get("webhook_url")
    if not webhook_url:
        raise HTTPException(status_code=400, detail="'webhook_url' is required.")

    req = IntegrationRequest(
        type=itype,
        webhook_url=webhook_url,
        username=body.get("username", "ProxyWatch"),
        events=body.get("events", ["alert.fired", "alert.resolved"]),
    )
    resp: IntegrationResponse = await integration_mgr.register(req)
    return resp.model_dump()


@app.get("/metrics")
async def get_metrics():
    return MetricsResponse(
        total_checks=await pool.total_checks_count(),
        current_pool_size=await pool.pool_size(),
        active_alerts=await alert_mgr.active_alert_count(),
        total_alerts=await alert_mgr.total_alert_count(),
        webhook_deliveries=await webhook_mgr.delivery_count(),
    ).model_dump()


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error."},
    )