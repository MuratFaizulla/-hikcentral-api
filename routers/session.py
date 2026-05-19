from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import state
from core import (
    _hik_call, _is_session_expired, _now_iso, _try_relogin,
    build_client_from_browser_capture, get_client,
)

router = APIRouter()


# ── Models ─────────────────────────────────────────────────────────────────

class SessionUpdate(BaseModel):
    base_url: str = Field("http://10.25.1.30")
    sid: str = Field(...)
    encrypted_aes_b64: str = Field(...)
    hostname: str = Field("10.25.1.30")
    token_key_num: int = Field(11, ge=1)


class SessionInfo(BaseModel):
    ok: bool
    sid: str
    aes_key_hex: str
    token_key_num: int


class LoginRequest(BaseModel):
    base_url: str = Field("http://10.25.1.30")
    username: str = Field("admin_trk")
    password: str = Field(...)


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/api/session", response_model=SessionInfo, tags=["Session"])
def update_session(s: SessionUpdate):
    """Перезаписать активную сессию (SID + зашифрованный AES ключ из браузерного localStorage)."""
    state._client = build_client_from_browser_capture(
        base_url=s.base_url, sid=s.sid, encrypted_aes_b64=s.encrypted_aes_b64,
        hostname=s.hostname, token_key_num=s.token_key_num,
    )
    state._client.save_session(state.SESSION_FILE)
    return SessionInfo(ok=True, sid=s.sid, aes_key_hex=state._client.aes_key_hex, token_key_num=s.token_key_num)


@router.get("/api/session", response_model=SessionInfo, tags=["Session"])
def session_info():
    """Текущее состояние сессии."""
    c = get_client()
    return SessionInfo(ok=True, sid=c.sid, aes_key_hex=c.aes_key_hex, token_key_num=c._tkn)


@router.post("/api/login", response_model=SessionInfo, tags=["Session"])
def auto_login(req: LoginRequest):
    """Автологин через headless-Chromium (Playwright)."""
    from hik.autologin import capture_session_sync
    try:
        captured = capture_session_sync(base_url=req.base_url, username=req.username, password=req.password)
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(500, f"autologin failed: {e}")

    state._saved_creds.update({"base_url": req.base_url, "username": req.username, "password": req.password})
    state._client = build_client_from_browser_capture(
        base_url=captured.base_url, sid=captured.sid,
        encrypted_aes_b64=captured.encrypted_aes_b64,
        hostname=captured.hostname, token_key_num=captured.token_key_num,
    )
    state._client.save_session(state.SESSION_FILE, creds={"username": req.username, "password": req.password})
    return SessionInfo(ok=True, sid=captured.sid, aes_key_hex=state._client.aes_key_hex, token_key_num=captured.token_key_num)


@router.get("/api/health", tags=["Session"])
def health():
    """Проверка живости сессии + статус авто-обновления."""
    try:
        c = get_client()
        watcher_ok = state._watch_status.get("status") in ("ok", "renewed")
        watcher_fresh = False
        if state._watch_status.get("last_checked"):
            age_s = (datetime.now(timezone.utc).astimezone() -
                     datetime.fromisoformat(state._watch_status["last_checked"])).total_seconds()
            watcher_fresh = age_s < 120
        if watcher_ok and watcher_fresh:
            return {"ok": True, "sid": c.sid, "watcher": state._watch_status}
        r = c.keep_alive()
        ok = r.get("ResponseStatus", {}).get("ErrorCode") == 0
        if ok:
            state._watch_status.update({"last_checked": _now_iso(), "status": "ok", "last_sid_prefix": c.sid[:8], "message": ""})
        elif _is_session_expired(r) and _try_relogin():
            c = get_client()
            now = _now_iso()
            state._watch_status.update({"status": "ok", "last_renewed": now, "last_checked": now, "last_sid_prefix": c.sid[:8], "message": ""})
            ok = True
        return {"ok": ok, "sid": c.sid, "watcher": state._watch_status}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@router.post("/api/cache/refresh", tags=["Session"])
def cache_refresh():
    """Принудительно сбросить все in-memory кэши."""
    state._all_persons_cache["data"] = None
    state._all_persons_cache["ts"] = 0.0
    state._stats_daily_cache["data"] = None
    state._stats_daily_cache["ts"] = 0.0
    state._presence_cache["data"] = None
    state._presence_cache["ts"] = 0.0
    state._today_records_cache["data"] = None
    state._today_records_cache["ts"] = 0.0
    state._elements_cache["data"] = None
    state._elements_cache["ts"] = 0.0
    state._gid_dept_cache.clear()
    state._gid_dept_cache_ts = 0.0
    return {"ok": True, "cleared": ["persons", "stats_daily", "presence", "today_records", "elements", "gid_dept"]}
