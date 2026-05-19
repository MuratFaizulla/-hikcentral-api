from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

import state
from core import _hik_call, _extract_records, decrypt_in_place, get_client
from cache import (
    _get_all_persons, _get_today_records,
    _ensure_gid_dept_cache, _update_gid_dept_cache, _pass_direction,
)

router = APIRouter()


@router.get("/api/stats/today", tags=["Stats"])
def stats_today():
    """Сводка: всего людей + количество проходов за сегодня."""
    out: dict = {}
    try:
        persons = _hik_call(lambda c: c.list_persons(page=1, page_size=1))
        pl = persons.get("ResponseStatus", {}).get("Data", {}).get("PersonList", {})
        out["total_persons"] = pl.get("TotalNum") or pl.get("TotalCount") or pl.get("Total")
    except Exception as e:
        out["total_persons_error"] = str(e)
    try:
        now = datetime.now(timezone.utc).astimezone()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        raw = _hik_call(lambda c: c.card_swipe_records(
            page=1, page_size=1,
            start_time=today_start.isoformat(timespec="seconds"),
            end_time=now.isoformat(timespec="seconds"),
        ))
        _, total = _extract_records(raw)
        out["today_records"] = total
    except Exception as e:
        out["today_records_error"] = str(e)
    return out


@router.get("/api/stats/daily", tags=["Stats"])
def stats_daily(days: int = Query(7, ge=1, le=31)):
    """Количество проходов по дням. Результат кешируется на 60 сек."""
    def _daily_valid() -> bool:
        return (
            state._stats_daily_cache["data"] is not None
            and state._stats_daily_cache["days"] == days
            and time.time() - state._stats_daily_cache["ts"] < state._STATS_CACHE_TTL_S
        )

    if _daily_valid():
        return state._stats_daily_cache["data"]

    with state._stats_daily_lock:
        if _daily_valid():
            return state._stats_daily_cache["data"]

        now = datetime.now(timezone.utc).astimezone()
        start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now

        records: list[dict] = []
        _p, _ps = 1, 500
        while True:
            try:
                raw = _hik_call(lambda c, __p=_p: c.card_swipe_records(
                    page=__p, page_size=_ps,
                    start_time=start.isoformat(timespec="seconds"),
                    end_time=end.isoformat(timespec="seconds"),
                ))
            except Exception as e:
                raise HTTPException(500, str(e))
            rs = raw.get("ResponseStatus", {})
            if rs and rs.get("ErrorCode", 0) != 0:
                return JSONResponse(status_code=400, content={"error": rs})
            batch, total = _extract_records(raw)
            if not batch:
                break
            records.extend(batch)
            if len(records) >= total or len(batch) < _ps:
                break
            _p += 1

        counter: Counter = Counter()
        for rec in records:
            t = rec.get("DeviceTime") or rec.get("EventTime") or ""
            if t and len(t) >= 10:
                counter[t[:10]] += 1
        series = [{"date": d, "count": counter[d]} for d in sorted(counter)]
        result = {
            "start": start.isoformat(), "end": end.isoformat(),
            "series": series, "total": sum(counter.values()),
        }
        state._stats_daily_cache["data"] = result
        state._stats_daily_cache["ts"] = time.time()
        state._stats_daily_cache["days"] = days
        return result


@router.get("/api/stats/presence", tags=["Stats"])
def stats_presence():
    """Текущее присутствие за сегодня: кто в школе, кто ушёл, разбивка по классам. Кешируется на 120 сек."""
    def _presence_valid() -> bool:
        return (
            state._presence_cache["data"] is not None
            and time.time() - state._presence_cache["ts"] < state._PRESENCE_CACHE_TTL_S
        )

    if _presence_valid():
        return state._presence_cache["data"]

    with state._presence_lock:
        if _presence_valid():
            return state._presence_cache["data"]

        _ensure_gid_dept_cache()
        now = datetime.now(timezone.utc).astimezone()

        try:
            all_records = _get_today_records()
            _update_gid_dept_cache(all_records)
        except Exception as e:
            raise HTTPException(500, str(e))

        person_map: dict[str, dict] = {}
        for rec in all_records:
            pid = (rec.get("Person") or {}).get("ID") or rec.get("CardNumber") or rec.get("ID", "?")
            key = str(pid)
            bi = (rec.get("Person") or {}).get("BaseInfo") or {}
            dt = rec.get("DeviceTime", "")
            el = rec.get("ElementName") or rec.get("CardReaderName") or ""

            if key not in person_map:
                person_map[key] = {
                    "person_id": (rec.get("Person") or {}).get("ID"),
                    "name": bi.get("GivenName") or bi.get("FullName") or f"ID {pid}",
                    "iin": bi.get("FamilyName"),
                    "card_code": bi.get("PersonCode"),
                    "dept": bi.get("FullPath") or "",
                    "first_entry": dt,
                    "last_pass": dt,
                    "last_element": el,
                    "zone": _pass_direction(el),
                    "pass_count": 1,
                }
            else:
                p = person_map[key]
                p["pass_count"] += 1
                if dt and (not p["first_entry"] or dt < p["first_entry"]):
                    p["first_entry"] = dt
                if dt and (not p["last_pass"] or dt > p["last_pass"]):
                    p["last_pass"] = dt
                    p["last_element"] = el
                    p["zone"] = _pass_direction(el)

        persons = list(person_map.values())

        dept_map: dict[str, dict] = {}
        for p in persons:
            fp = p.get("dept", "") or ""
            parts = [s.strip() for s in fp.split(">") if s.strip()]
            dept_label = parts[-1] if parts else "—"
            if dept_label not in dept_map:
                dept_map[dept_label] = {"dept": dept_label, "full_path": fp, "in_school": 0, "left": 0, "total": 0}
            dept_map[dept_label]["total"] += 1
            if p["zone"] == "in":
                dept_map[dept_label]["in_school"] += 1
            elif p["zone"] == "out":
                dept_map[dept_label]["left"] += 1

        recent = sorted(all_records, key=lambda r: r.get("DeviceTime", ""), reverse=True)[:30]
        result = {
            "as_of": now.isoformat(timespec="seconds"),
            "total_records": len(all_records),
            "total_came": len(persons),
            "in_school": sum(1 for p in persons if p["zone"] == "in"),
            "left_school": sum(1 for p in persons if p["zone"] == "out"),
            "unknown": sum(1 for p in persons if p["zone"] == "unknown"),
            "by_dept": sorted(dept_map.values(), key=lambda x: -x["total"]),
            "recent_passes": recent,
        }
        state._presence_cache["data"] = result
        state._presence_cache["ts"] = time.time()
        return result


@router.get("/api/stats/late", tags=["Stats"])
def stats_late(
    date: Optional[str] = Query(None, description="Дата YYYY-MM-DD, по умолчанию сегодня"),
    after: str = Query("08:30", description="Время в формате HH:MM — граница опоздания"),
    element_ids: Optional[str] = Query(None, description="Фильтр по точкам доступа (вход)"),
):
    """Отчёт опоздавших за день. Первый проход каждого человека — время прихода."""
    if date:
        try:
            _d = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "date должен быть в формате YYYY-MM-DD")
    else:
        _d = datetime.now(timezone.utc).astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    try:
        _after_h, _after_m = [int(x) for x in after.split(":")]
    except Exception:
        raise HTTPException(400, "after должен быть в формате HH:MM")

    tz = datetime.now(timezone.utc).astimezone().tzinfo
    day_start = _d.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
    day_end   = _d.replace(hour=23, minute=59, second=59, microsecond=0, tzinfo=tz)
    cutoff_dt = _d.replace(hour=_after_h, minute=_after_m, second=0, microsecond=0, tzinfo=tz)

    _eids = element_ids or ""
    today_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    is_today = (_d.strftime("%Y-%m-%d") == today_str)

    if is_today and not _eids:
        try:
            all_records: list = _get_today_records()
        except Exception as e:
            raise HTTPException(500, str(e))
    else:
        _ps = 500
        all_records = []
        _p = 1
        while True:
            _cur = _p
            try:
                raw = _hik_call(lambda c, __p=_cur: c.card_swipe_records(
                    page=__p, page_size=_ps,
                    start_time=day_start.isoformat(timespec="seconds"),
                    end_time=day_end.isoformat(timespec="seconds"),
                    element_ids=_eids,
                ))
            except Exception as e:
                raise HTTPException(500, str(e))
            batch, _total = _extract_records(raw)
            if not batch:
                break
            all_records.extend(batch)
            if len(all_records) >= _total or len(batch) < _ps:
                break
            _p += 1
        decrypt_in_place(all_records, get_client().aes_key_hex)

    first_pass: dict[str, dict] = {}
    for r in all_records:
        pid = str((r.get("Person") or {}).get("ID") or r.get("CardNumber") or "")
        if not pid:
            continue
        dt_raw = r.get("DeviceTime") or ""
        if not dt_raw:
            continue
        if pid not in first_pass or dt_raw < first_pass[pid]["time"]:
            b = (r.get("Person") or {}).get("BaseInfo") or {}
            first_pass[pid] = {
                "person_id": (r.get("Person") or {}).get("ID"),
                "name": b.get("GivenName") or b.get("FullName") or f"ID {pid}",
                "iin": b.get("FamilyName") or "",
                "code": b.get("PersonCode") or "",
                "dept": b.get("FullPath") or "",
                "time": dt_raw,
                "element": r.get("ElementName") or "",
            }

    try:
        everyone = _get_all_persons(get_client())
    except Exception:
        everyone = []

    on_time: list = []
    late: list = []
    absent: list = []
    seen_pids: set = set()

    for pid_str, info in first_pass.items():
        seen_pids.add(pid_str)
        try:
            arr = datetime.fromisoformat(info["time"])
            if arr.tzinfo is None:
                arr = arr.replace(tzinfo=tz)
        except Exception:
            arr = cutoff_dt

        entry = {
            "person_id": info["person_id"],
            "name": info["name"],
            "iin": info["iin"],
            "code": info["code"],
            "dept": info["dept"],
            "arrived_at": info["time"],
            "element": info["element"],
        }
        if arr <= cutoff_dt:
            on_time.append(entry)
        else:
            minutes_late = int((arr - cutoff_dt).total_seconds() / 60)
            late.append({**entry, "minutes_late": minutes_late})

    for p in everyone:
        pid_str = str(p.get("ID") or "")
        if not pid_str or pid_str in seen_pids:
            continue
        b = p.get("BaseInfo") or {}
        if b.get("DisableStatus") == 1:
            continue
        absent.append({
            "person_id": p.get("ID"),
            "name": b.get("GivenName") or b.get("FullName") or f"ID {pid_str}",
            "iin": b.get("FamilyName") or "",
            "code": b.get("PersonCode") or "",
            "dept": b.get("FullPath") or "",
        })

    late.sort(key=lambda x: x["arrived_at"])
    on_time.sort(key=lambda x: x["arrived_at"])
    absent.sort(key=lambda x: x["name"])

    return {
        "date": _d.strftime("%Y-%m-%d"),
        "cutoff": after,
        "total_persons": len(everyone),
        "came": len(on_time) + len(late),
        "on_time_count": len(on_time),
        "late_count": len(late),
        "absent_count": len(absent),
        "on_time": on_time,
        "late": late,
        "absent": absent,
    }
