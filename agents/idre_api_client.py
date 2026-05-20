"""Typed wrappers around IDRE's report endpoints."""
from __future__ import annotations
import os
import time
from dataclasses import dataclass
from typing import Any
import requests

IDRE_BASE_URL = os.environ.get("IDRE_BASE_URL", "http://127.0.0.1:3000")
DEFAULT_TIMEOUT_S = 300

KNOWN_ENDPOINTS = {
    "due-dates": "/api/reports/due-dates",
    "outstanding-payments": "/api/reports/outstanding-payments",
    "case-balance": "/api/reports/case-balance",
    "dashboard-stats": "/api/reports/dashboard-stats",
    "cms-payments": "/api/reports/cms-payments",
    "unpaid-disputes": "/api/reports/unpaid-disputes",
    "idre-payouts": "/api/reports/idre-payouts",
    # Day 9 additions
    "case-analytics": "/api/reports/case-analytics",
    "team-performance": "/api/reports/team-performance",
    "recent-activity": "/api/reports/recent-activity",
    "auditing/daily-funds": "/api/reports/auditing/daily-funds",
    "auditing/daily-transactions": "/api/reports/auditing/daily-transactions",
    # Note: payment-variance not on staging
}


@dataclass
class IdreApiResponse:
    status_code: int
    body: Any
    latency_ms: float
    headers: dict

    def to_dict(self) -> dict:
        return {"status_code": self.status_code, "body": self.body, "latency_ms": self.latency_ms}


class IdreApiClient:
    def __init__(self, session: requests.Session | None = None, base_url: str = IDRE_BASE_URL):
        self.base_url = base_url
        self.session = session or self._auto_session()
        self._cache: dict[tuple, IdreApiResponse] = {}
        self._cache_ttl_s = 60.0
        self._cache_stamps: dict[tuple, float] = {}

    def _auto_session(self) -> requests.Session:
        s = requests.Session()
        r = s.get(f"{self.base_url}/api/dev/auto-login", allow_redirects=True, timeout=30)
        r.raise_for_status()
        return s

    def call(self, report_id: str, params: dict) -> dict:
        if report_id not in KNOWN_ENDPOINTS:
            raise KeyError(f"unknown report_id: {report_id}")
        key = (report_id, tuple(sorted((k, str(v)) for k, v in params.items())))
        now = time.monotonic()
        if key in self._cache and (now - self._cache_stamps[key]) < self._cache_ttl_s:
            return self._cache[key].to_dict()
        path = KNOWN_ENDPOINTS[report_id]
        url = self.base_url + path
        start = time.monotonic()
        r = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT_S)
        latency = (time.monotonic() - start) * 1000
        try:
            body = r.json()
        except Exception:
            body = {"_raw": r.text[:2000]}
        resp = IdreApiResponse(r.status_code, body, latency, dict(r.headers))
        self._cache[key] = resp
        self._cache_stamps[key] = now
        return resp.to_dict()


def idre_api_client_node(state: dict) -> dict:
    """LangGraph node: only runs when router_decision.path == 'known'."""
    rd = state.get("router_decision", {})
    if rd.get("path") != "known":
        return state
    client = IdreApiClient()
    resp = client.call(rd["report"], rd.get("parameters", {}))
    return {**state, "idre_api_response": resp}
