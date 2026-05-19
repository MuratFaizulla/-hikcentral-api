"""Глобальное мутабельное состояние приложения. Никакой логики — только данные и локи."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, Optional

from hik.client import HikClient

SESSION_FILE = Path(__file__).parent / "session.json"

# ── HikClient ──────────────────────────────────────────────────────────────
_client: Optional[HikClient] = None
_client_lock = threading.Lock()
_saved_creds: dict[str, str] = {}

# ── Session watcher ────────────────────────────────────────────────────────
_watch_status: dict[str, Any] = {
    "status": "starting",
    "last_checked": None,
    "last_renewed": None,
    "last_sid_prefix": None,
    "message": "",
    "check_interval_s": 120,
}
_RETRY_INTERVAL_ERROR_S = 60

# ── SSE ────────────────────────────────────────────────────────────────────
_sse_clients: list[asyncio.Queue] = []
_sse_clients_lock = threading.Lock()
_sse_loop: Optional[asyncio.AbstractEventLoop] = None
_sse_last_seen: Optional[str] = None
_sse_seen_ids: set[str] = set()
_SSE_SEEN_MAX = 2000

# ── Persons cache ──────────────────────────────────────────────────────────
_all_persons_cache: dict[str, Any] = {"data": None, "ts": 0.0, "aes_key": ""}
_all_persons_lock = threading.Lock()
_CACHE_TTL_S = 300

_gid_dept_cache: dict[int, str] = {}
_gid_dept_cache_ts: float = 0.0
_GID_CACHE_TTL_S = 300

# ── Elements / areas / event-types caches ─────────────────────────────────
_elements_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_elements_lock = threading.Lock()
_ELEMENTS_CACHE_TTL_S = 60

_areas_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_AREAS_CACHE_TTL_S = 120

_event_types_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_EVENT_TYPES_CACHE_TTL_S = 3600

# ── Stats caches ───────────────────────────────────────────────────────────
_stats_daily_cache: dict[str, Any] = {"data": None, "ts": 0.0, "days": 0}
_stats_daily_lock = threading.Lock()
_STATS_CACHE_TTL_S = 60

_presence_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_presence_lock = threading.Lock()
_PRESENCE_CACHE_TTL_S = 120

_today_records_cache: dict[str, Any] = {"data": None, "ts": 0.0, "date": ""}
_today_records_lock = threading.Lock()
_TODAY_RECORDS_TTL_S = 60
