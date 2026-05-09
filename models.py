from __future__ import annotations
from typing import Optional, List
from pydantic import BaseModel


class ConfigRequest(BaseModel):
    check_interval_seconds: int
    request_timeout_ms: int
    model_config = {"extra": "ignore"}


class ConfigResponse(BaseModel):
    check_interval_seconds: int
    request_timeout_ms: int


class CheckRecord(BaseModel):
    checked_at: str
    status: str


class ProxySummary(BaseModel):
    id: str
    url: str
    status: str
    last_checked_at: Optional[str] = None
    consecutive_failures: int = 0


class ProxyDetail(BaseModel):
    id: str
    url: str
    status: str
    last_checked_at: Optional[str] = None
    consecutive_failures: int = 0
    total_checks: int = 0
    uptime_percentage: float = 0.0
    history: List[CheckRecord] = []


class PoolResponse(BaseModel):
    total: int
    up: int
    down: int
    failure_rate: float
    proxies: List[ProxySummary]


class IngestionResponse(BaseModel):
    accepted: int
    proxies: List[ProxySummary]


class AlertObject(BaseModel):
    alert_id: str
    status: str
    failure_rate: float
    total_proxies: int
    failed_proxies: int
    failed_proxy_ids: List[str]
    threshold: float
    fired_at: str
    resolved_at: Optional[str]
    message: str


class WebhookRegisterRequest(BaseModel):
    url: str
    model_config = {"extra": "ignore"}


class WebhookRegisterResponse(BaseModel):
    webhook_id: str
    url: str


class IntegrationRequest(BaseModel):
    type: str
    webhook_url: str
    username: str
    events: List[str]
    model_config = {"extra": "ignore"}


class IntegrationResponse(BaseModel):
    integration_id: str
    type: str
    webhook_url: str
    username: str
    events: List[str]


class MetricsResponse(BaseModel):
    total_checks: int
    current_pool_size: int
    active_alerts: int
    total_alerts: int
    webhook_deliveries: int