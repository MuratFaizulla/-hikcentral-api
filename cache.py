"""Кэширующие аксессоры с double-checked locking."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import state
from core import _hik_call, decrypt_in_place, get_client
from hik.client import HikClient


# ── Persons cache ──────────────────────────────────────────────────────────

def _build_all_persons_cache(c: HikClient) -> list[dict]:
    all_persons: list[dict] = []
    page, page_size = 1, 200
    while True:
        raw = c.list_persons(page=page, page_size=page_size)
        rs = raw.get("ResponseStatus", {})
        if rs.get("ErrorCode") != 0:
            raise RuntimeError(f"ошибка Hik при загрузке всех людей: {rs}")
        pl = rs.get("Data", {}).get("PersonList", {})
        persons = pl.get("Person", [])
        if not persons:
            break
        decrypt_in_place(persons, c.aes_key_hex)
        all_persons.extend(persons)
        total = pl.get("TotalNum") or 0
        if len(all_persons) >= total or len(persons) < page_size:
            break
        page += 1
    return all_persons


def _all_persons_cache_valid(c: HikClient) -> bool:
    return (
        state._all_persons_cache["data"] is not None
        and state._all_persons_cache["aes_key"] == c.aes_key_hex
        and time.time() - state._all_persons_cache["ts"] < state._CACHE_TTL_S
    )


def _get_all_persons(c: HikClient) -> list[dict]:
    if _all_persons_cache_valid(c):
        return state._all_persons_cache["data"]  # type: ignore[return-value]
    with state._all_persons_lock:
        if _all_persons_cache_valid(c):
            return state._all_persons_cache["data"]  # type: ignore[return-value]
        data = _build_all_persons_cache(c)
        state._all_persons_cache["data"] = data
        state._all_persons_cache["ts"] = time.time()
        state._all_persons_cache["aes_key"] = c.aes_key_hex
        return data


# ── Gid → dept cache ───────────────────────────────────────────────────────

def _ensure_gid_dept_cache() -> None:
    now = time.time()
    if state._gid_dept_cache and now - state._gid_dept_cache_ts < state._GID_CACHE_TTL_S:
        return
    try:
        raw = _hik_call(lambda c: c.person_groups())
        groups = (
            raw.get("ResponseStatus", {})
               .get("Data", {})
               .get("PersonGroupList", {})
               .get("PersonGroup", [])
        )
        if groups:
            for g in groups:
                gid  = g.get("ID")
                name = (g.get("BaseInfo") or {}).get("Name", "")
                if gid and name:
                    state._gid_dept_cache[gid] = name
            state._gid_dept_cache_ts = now
    except Exception:
        pass


def _update_gid_dept_cache(records: list[dict]) -> None:
    for rec in records:
        bi  = (rec.get("Person") or {}).get("BaseInfo") or {}
        gid = bi.get("PersonGroupID")
        fp  = bi.get("FullPath") or ""
        parts = [s.strip() for s in fp.split(">") if s.strip()]
        dept = parts[-1] if parts else ""
        if gid and dept:
            state._gid_dept_cache[gid] = dept


# ── Pass direction ─────────────────────────────────────────────────────────

def _pass_direction(element_name: str) -> str:
    n = (element_name or "").lower()
    if "-in-" in n or "_in_" in n:
        return "in"
    if "-out-" in n or "_out_" in n:
        return "out"
    return "unknown"


# ── Today records cache ────────────────────────────────────────────────────

def _today_records_cache_valid() -> bool:
    today_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    return (
        state._today_records_cache["data"] is not None
        and state._today_records_cache["date"] == today_str
        and time.time() - state._today_records_cache["ts"] < state._TODAY_RECORDS_TTL_S
    )


def _get_today_records() -> list[dict]:
    """Все расшифрованные записи за сегодня. Кэш 60 сек."""
    from core import _extract_records  # local to avoid top-level cycle
    if _today_records_cache_valid():
        return state._today_records_cache["data"]  # type: ignore[return-value]
    with state._today_records_lock:
        if _today_records_cache_valid():
            return state._today_records_cache["data"]  # type: ignore[return-value]

        now = datetime.now(timezone.utc).astimezone()
        today_str = now.strftime("%Y-%m-%d")
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        all_records: list[dict] = []
        _p, _ps = 1, 500
        while True:
            raw = _hik_call(lambda c, __p=_p: c.card_swipe_records(
                page=__p, page_size=_ps,
                start_time=today_start.isoformat(timespec="seconds"),
                end_time=now.isoformat(timespec="seconds"),
            ))
            batch, grand_total = _extract_records(raw)
            if not batch:
                break
            all_records.extend(batch)
            if len(batch) < _ps or (grand_total > _ps and len(all_records) >= grand_total):
                break
            _p += 1

        decrypt_in_place(all_records, get_client().aes_key_hex)
        state._today_records_cache["data"] = all_records
        state._today_records_cache["ts"] = time.time()
        state._today_records_cache["date"] = today_str
        return all_records
