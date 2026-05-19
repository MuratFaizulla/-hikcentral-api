"""Фоновые потоки: session watcher, SSE event poller, cache prewarm."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import state
from core import _extract_records, _hik_call, _is_session_expired, _now_iso, _try_relogin, decrypt_in_place, get_client, load_or_init_client
from cache import _get_all_persons, _get_today_records, _update_gid_dept_cache
from routers.webhooks import fire_webhooks, has_active_webhooks


# ── SSE ────────────────────────────────────────────────────────────────────

def _sse_broadcast(records: list[dict]) -> None:
    if not state._sse_loop:
        return
    payload = json.dumps(records, ensure_ascii=False)
    with state._sse_clients_lock:
        clients = list(state._sse_clients)
    for q in clients:
        try:
            state._sse_loop.call_soon_threadsafe(q.put_nowait, payload)
        except Exception:
            pass


def _record_dedup_key(r: dict) -> str:
    t   = r.get("DeviceTime") or r.get("EventTime") or ""
    eid = str(r.get("ElementID") or r.get("DoorID") or "")
    pid = str((r.get("Person") or {}).get("ID") or "")
    return f"{t}|{eid}|{pid}"


# ── Session watcher ────────────────────────────────────────────────────────

def _do_check_and_renew() -> None:
    now = _now_iso()
    state._watch_status["last_checked"] = now

    if state._client is None:
        try:
            state._client = load_or_init_client()
        except Exception:
            pass

    if state._client is None:
        state._watch_status["status"] = "renewing"
        state._watch_status["message"] = "Нет сессии, запускаю автологин…"
        if _try_relogin():
            c2 = get_client()
            state._watch_status.update({"status": "ok", "last_renewed": now, "last_sid_prefix": c2.sid[:8], "message": ""})
        else:
            state._watch_status["status"] = "error"
        return

    try:
        c = get_client()
        r = c.keep_alive()
        ok = r.get("ResponseStatus", {}).get("ErrorCode") == 0
        if ok:
            state._watch_status.update({"status": "ok", "last_sid_prefix": c.sid[:8], "message": ""})
        elif _is_session_expired(r):
            state._watch_status.update({"status": "renewing", "message": "Сессия истекла, запускаю автологин…"})
            if _try_relogin():
                c2 = get_client()
                state._watch_status.update({"status": "ok", "last_renewed": now, "last_sid_prefix": c2.sid[:8], "message": ""})
            else:
                state._watch_status["status"] = "error"
        else:
            state._watch_status.update({"status": "error", "message": f"KeepAlive ErrorCode={r.get('ResponseStatus', {}).get('ErrorCode')}"})
    except Exception as e:
        state._watch_status.update({"status": "error", "message": str(e)})


def _session_watcher_loop() -> None:
    _do_check_and_renew()
    while True:
        interval = (
            state._RETRY_INTERVAL_ERROR_S
            if state._watch_status.get("status") == "error"
            else state._watch_status["check_interval_s"]
        )
        time.sleep(interval)
        _do_check_and_renew()


# ── SSE event poller ───────────────────────────────────────────────────────

def _event_poller_loop() -> None:
    POLL_INTERVAL = 10
    LOOKBACK_SEC  = 30

    while True:
        time.sleep(POLL_INTERVAL)

        with state._sse_clients_lock:
            has_sse = bool(state._sse_clients)
        if not has_sse and not has_active_webhooks():
            continue
        if state._client is None:
            continue

        try:
            now = datetime.now(timezone.utc).astimezone()
            if state._sse_last_seen:
                try:
                    since_dt = datetime.fromisoformat(state._sse_last_seen) - timedelta(seconds=5)
                    since = since_dt.isoformat(timespec="seconds")
                except Exception:
                    since = (now - timedelta(seconds=LOOKBACK_SEC)).isoformat(timespec="seconds")
            else:
                since = (now - timedelta(seconds=LOOKBACK_SEC)).isoformat(timespec="seconds")

            raw = _hik_call(lambda c: c.card_swipe_records(
                page=1, page_size=200,
                start_time=since,
                end_time=now.isoformat(timespec="seconds"),
            ))
            records, _ = _extract_records(raw)
            if not records:
                continue

            decrypt_in_place(records, get_client().aes_key_hex)
            _update_gid_dept_cache(records)

            max_time = max((r.get("DeviceTime", "") for r in records), default="")
            if max_time and (not state._sse_last_seen or max_time > state._sse_last_seen):
                state._sse_last_seen = max_time

            if len(state._sse_seen_ids) > state._SSE_SEEN_MAX:
                state._sse_seen_ids.clear()
            new_records = []
            for r in records:
                key = _record_dedup_key(r)
                if key not in state._sse_seen_ids:
                    state._sse_seen_ids.add(key)
                    new_records.append(r)

            if not new_records:
                continue

            state._presence_cache["data"] = None
            state._today_records_cache["data"] = None
            _sse_broadcast(new_records)
            fire_webhooks(new_records)

        except Exception:
            pass


# ── Cache prewarm ──────────────────────────────────────────────────────────

def _prewarm_caches() -> None:
    time.sleep(3)
    try:
        _get_all_persons(get_client())
    except Exception:
        pass
    try:
        _get_today_records()
    except Exception:
        pass


# ── Start threads ──────────────────────────────────────────────────────────

threading.Thread(target=_session_watcher_loop, daemon=True, name="session-watcher").start()
threading.Thread(target=_event_poller_loop,    daemon=True, name="event-poller").start()
threading.Thread(target=_prewarm_caches,       daemon=True, name="cache-prewarm").start()
