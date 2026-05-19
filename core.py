"""Клиент HikCentral, авторизация, шифрование, общие утилиты."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

import state
from hik.client import HikClient, build_client_from_browser_capture
from hik.crypto import decrypt_field
from settings import settings

ENCRYPTED_PERSON_FIELDS = {
    "FamilyName", "GivenName", "FullName", "MiddleName",
    "PhoneNum", "CertificateNo", "Email", "Address", "Remark",
    "CardNo", "PassengerName",
}


# ── Client bootstrap ───────────────────────────────────────────────────────

def load_or_init_client() -> HikClient:
    if state.SESSION_FILE.exists():
        return HikClient.from_session_file(state.SESSION_FILE)
    if not settings.hik_sid or not settings.hik_encrypted_aes_key:
        raise RuntimeError(
            "Нет session.json. Залогинься через POST /api/session или укажи "
            "HIK_SID + HIK_ENCRYPTED_AES_KEY."
        )
    return build_client_from_browser_capture(
        base_url=settings.hik_base_url,
        sid=settings.hik_sid,
        encrypted_aes_b64=settings.hik_encrypted_aes_key,
        hostname=settings.hik_hostname,
        token_key_num=11,
    )


def get_client() -> HikClient:
    with state._client_lock:
        if state._client is None:
            state._client = load_or_init_client()
        return state._client


def _is_session_expired(resp: dict) -> bool:
    code = resp.get("ResponseStatus", {}).get("ErrorCode")
    return code in (222, 200, 216, 220)


def _try_relogin() -> bool:
    saved = HikClient.load_saved_creds(state.SESSION_FILE)
    username = state._saved_creds.get("username") or saved.get("username") or settings.hik_username
    password = state._saved_creds.get("password") or saved.get("password") or settings.hik_password
    base_url = (
        state._saved_creds.get("base_url") or saved.get("base_url") or settings.hik_base_url
    )
    if not username or not password:
        state._watch_status["message"] = "Нет credentials для авто-релогина"
        return False
    try:
        from hik.direct_login import direct_login_or_playwright
        state._watch_status["message"] = f"Автологин: {username}@{base_url}…"
        c = direct_login_or_playwright(base_url=base_url, username=username, password=password)
        new_client = build_client_from_browser_capture(
            base_url=c.base_url, sid=c.sid,
            encrypted_aes_b64=c.encrypted_aes_b64,
            hostname=c.hostname, token_key_num=c.token_key_num,
        )
        with state._client_lock:
            state._client = new_client
            state._all_persons_cache["data"] = None
        state._saved_creds["username"] = username
        state._saved_creds["password"] = password
        state._saved_creds["base_url"] = base_url
        new_client.save_session(state.SESSION_FILE, creds={"username": username, "password": password})
        return True
    except Exception as e:
        state._watch_status["message"] = f"Автологин не удался: {e}"
        return False


def _hik_call(fn: Callable[[HikClient], dict]) -> dict:
    result = fn(get_client())
    if _is_session_expired(result) and _try_relogin():
        result = fn(get_client())
    return result


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ── Decryption ─────────────────────────────────────────────────────────────

def decrypt_in_place(obj: Any, aes_key: str) -> Any:
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k in ENCRYPTED_PERSON_FIELDS and isinstance(v, str) and v:
                obj[k] = decrypt_field(v, aes_key)
            else:
                decrypt_in_place(v, aes_key)
    elif isinstance(obj, list):
        for it in obj:
            decrypt_in_place(it, aes_key)
    return obj


# ── Records extraction ─────────────────────────────────────────────────────

def _extract_records(raw: dict) -> tuple[list, int]:
    for container in [
        raw,
        raw.get("ResponseStatus", {}).get("Data", {}),
        raw.get("Data", {}),
    ]:
        lst = container.get("CardSwipeRecordsList", {})
        if lst:
            items = lst.get("CardSwipeRecord", [])
            if isinstance(items, dict):
                items = [items]
            total = int(lst.get("TotalNum", lst.get("TotalCnt", len(items))))
            return items, total
    return [], 0


# ── Person query resolver ──────────────────────────────────────────────────

def _resolve_person_query(
    person_name: Optional[str],
    person_id: Optional[int],
) -> tuple[Optional[int], list[int], Optional[str]]:
    """
    Разрешает person_name через кэш персон (ИИН зашифрован — HikCentral его не видит).
    Возвращает (single_pid, multi_pids, fallback_name).
    """
    if not person_name or person_id is not None:
        return person_id, [], person_name

    try:
        from cache import _get_all_persons
        q = person_name.strip().lower()
        everyone = _get_all_persons(get_client())
        matched = [
            p for p in everyone
            if q in " ".join([
                str(p.get("ID", "")),
                str((p.get("BaseInfo") or {}).get("PersonCode", "")),
                str((p.get("BaseInfo") or {}).get("FamilyName", "")),
                str((p.get("BaseInfo") or {}).get("GivenName", "")),
                str((p.get("BaseInfo") or {}).get("FullName", "")),
            ]).lower()
        ]
        if len(matched) == 1 and matched[0].get("ID"):
            return matched[0]["ID"], [], None
        if 1 < len(matched) <= 20:
            return None, [p["ID"] for p in matched if p.get("ID")], None
    except Exception:
        logger.debug("person query resolve failed", exc_info=True)
    return None, [], person_name
