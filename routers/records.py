from __future__ import annotations

import concurrent.futures
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import JSONResponse

import state
from core import (
    _hik_call, _extract_records, _resolve_person_query, decrypt_in_place, get_client,
)

router = APIRouter()

_PAGE_SIZE = 500
_MAX_WORKERS = 6


def _fetch_all_pages(
    start_time: Optional[str],
    end_time: Optional[str],
    eids: str,
    pid: Optional[int] = None,
    pname: Optional[str] = None,
) -> tuple[list, int]:
    """Страница 1 — узнать total, остальные страницы — параллельно."""

    def _fetch(pg: int) -> dict:
        return _hik_call(
            lambda c, __p=pg: c.card_swipe_records(
                page=__p, page_size=_PAGE_SIZE,
                start_time=start_time, end_time=end_time,
                person_id=pid, person_name=pname,
                element_ids=eids,
            )
        )

    raw1 = _fetch(1)
    if raw1.get("ResponseStatus", {}).get("ErrorCode", 0) != 0:
        return [], 0

    batch1, total = _extract_records(raw1)
    if not batch1 or len(batch1) >= total:
        return batch1, total

    num_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
    remaining = list(range(2, num_pages + 1))
    page_results: dict[int, list] = {1: batch1}

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(remaining))) as ex:
        futures = {ex.submit(_fetch, pg): pg for pg in remaining}
        for fut in concurrent.futures.as_completed(futures):
            pg = futures[fut]
            try:
                batch, _ = _extract_records(fut.result())
                page_results[pg] = batch
            except Exception:
                page_results[pg] = []

    all_records: list = []
    for pg in range(1, num_pages + 1):
        all_records.extend(page_results.get(pg, []))

    return all_records, total


def _record_dedup_key(r: dict) -> str:
    t   = r.get("DeviceTime") or r.get("EventTime") or ""
    eid = str(r.get("ElementID") or r.get("DoorID") or "")
    pid = str((r.get("Person") or {}).get("ID") or "")
    return f"{t}|{eid}|{pid}"


def _hik_data(raw: dict) -> dict:
    import json as _json
    rs = raw.get("ResponseStatus", raw)
    data = rs.get("Data", rs)
    if isinstance(data, str):
        try:
            data = _json.loads(data)
        except Exception:
            data = {}
    return data if isinstance(data, dict) else {}


@router.get("/api/records", tags=["Records"])
def list_records(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=1000),
    fetch_all: bool = Query(False, description="Загрузить ВСЕ записи (бэкенд сам обходит все страницы)"),
    start_time: Optional[str] = Query(None, description="ISO 8601, e.g. 2026-05-14T00:00:00+05:00"),
    end_time: Optional[str] = Query(None),
    person_id: Optional[int] = Query(None),
    person_name: Optional[str] = Query(None),
    element_ids: Optional[str] = Query(None, description="ID точек доступа через запятую"),
):
    """Записи проходов (события доступа). fetch_all=true — вернуть весь список без пагинации."""
    _eids = element_ids or ""
    if not start_time and not end_time:
        _now = datetime.now(timezone.utc).astimezone()
        start_time = _now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        end_time = _now.isoformat(timespec="seconds")

    _pid, _multi_pids, _pname = _resolve_person_query(person_name, person_id)

    if fetch_all:
        try:
            if _multi_pids:
                # несколько персон — параллельно по каждому pid
                all_records: list = []
                _total = 0
                seen_ids: set = set()
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(_multi_pids))) as ex:
                    futures = {
                        ex.submit(_fetch_all_pages, start_time, end_time, _eids, pid, None): pid
                        for pid in _multi_pids
                    }
                    for fut in concurrent.futures.as_completed(futures):
                        _batch, _t = fut.result()
                        _total += _t
                        for r in _batch:
                            _key = _record_dedup_key(r)
                            if _key not in seen_ids:
                                seen_ids.add(_key)
                                all_records.append(r)
            else:
                all_records, _total = _fetch_all_pages(start_time, end_time, _eids, _pid, _pname)
        except Exception as e:
            raise HTTPException(500, str(e))

        decrypt_in_place(all_records, get_client().aes_key_hex)
        return {"page": 1, "page_size": len(all_records), "total": _total, "records": all_records}

    _paginated_pid = _multi_pids[0] if _multi_pids else _pid
    _paginated_pname = None if _multi_pids else _pname
    try:
        raw = _hik_call(lambda c: c.card_swipe_records(
            page=page, page_size=page_size,
            start_time=start_time, end_time=end_time,
            person_id=_paginated_pid, person_name=_paginated_pname,
            element_ids=_eids,
        ))
    except Exception as e:
        raise HTTPException(500, str(e))

    rs = raw.get("ResponseStatus", {})
    if rs and rs.get("ErrorCode", 0) != 0:
        return JSONResponse(status_code=400, content={"error": rs, "raw": raw})

    records, total = _extract_records(raw)
    decrypt_in_place(records, get_client().aes_key_hex)
    return {"page": page, "page_size": page_size, "total": total, "records": records}


def _pass_direction(element_name: str, card_reader_name: str) -> str:
    name = (element_name or card_reader_name or "").lower()
    if "-in-" in name or "_in_" in name or re.search(r'\bin\b', name):
        return "in"
    if "-out-" in name or "_out_" in name or re.search(r'\bout\b', name):
        return "out"
    return "unknown"


def _group_by_person_py(records: list[dict]) -> list[dict]:
    """Python-аналог groupByPerson из фронтенда."""
    seen: dict[str, dict] = {}
    for r in records:
        pid = (r.get("Person") or {}).get("ID")
        key = f"p{pid}" if pid else f"c{r.get('CardNumber') or id(r)}"
        b = (r.get("Person") or {}).get("BaseInfo") or {}
        name = b.get("GivenName") or b.get("FullName") or (f"ID {pid}" if pid else "—")
        dir_ = _pass_direction(r.get("ElementName", ""), r.get("CardReaderName", ""))
        dt = r.get("DeviceTime") or ""
        if key not in seen:
            seen[key] = {
                "person_id": pid,
                "name": name,
                "iin": b.get("FamilyName") or "",
                "code": b.get("PersonCode") or "",
                "dept": b.get("FullPath") or "",
                "first_entry": dt,
                "last_pass": dt,
                "last_element": r.get("ElementName") or r.get("CardReaderName") or "",
                "zone": dir_,
                "pass_count": 1,
            }
        else:
            g = seen[key]
            g["pass_count"] += 1
            if dt:
                if not g["first_entry"] or dt < g["first_entry"]:
                    g["first_entry"] = dt
                if not g["last_pass"] or dt > g["last_pass"]:
                    g["last_pass"] = dt
                    g["last_element"] = r.get("ElementName") or r.get("CardReaderName") or ""
                    g["zone"] = dir_
    return sorted(seen.values(), key=lambda x: x.get("last_pass", ""), reverse=True)


def _fetch_photo_safe(client, *, person_id=None, snap_url=None) -> bytes | None:
    try:
        if snap_url:
            data = client.get_picture(snap_url)
            if data and len(data) > 500:
                return data
        if person_id:
            data = client.get_photo(person_id)
            if data and len(data) > 500:
                return data
    except Exception:
        pass
    return None


def _make_xl_image(photo_bytes: bytes, w: int = 100, h: int = 130):
    try:
        from openpyxl.drawing.image import Image as _XLImg
        from PIL import Image as _PIL
        import io as _io
        pil = _PIL.open(_io.BytesIO(photo_bytes)).convert("RGB")
        pil.thumbnail((w, h), _PIL.LANCZOS)
        buf = _io.BytesIO()
        pil.save(buf, format="JPEG", quality=90)
        buf.seek(0)
        xl = _XLImg(buf)
        xl.width = pil.width
        xl.height = pil.height
        return xl
    except Exception:
        return None


@router.get("/api/records/export.xlsx", tags=["Records"])
def export_records_xlsx(
    start_time: Optional[str] = Query(None),
    end_time: Optional[str] = Query(None),
    person_id: Optional[int] = Query(None),
    person_name: Optional[str] = Query(None),
    element_ids: Optional[str] = Query(None),
    with_photos: bool = Query(False, description="Вставить фото в таблицу"),
    grouped: bool = Query(False, description="Группировать по персонам (режим 'день')"),
):
    """Выгрузить проходы в Excel. grouped=true → сводка по людям; with_photos=true → вставить фото."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    if not start_time and not end_time:
        _now = datetime.now(timezone.utc).astimezone()
        start_time = _now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        end_time = _now.isoformat(timespec="seconds")

    _pid, _multi_pids_export, _pname = _resolve_person_query(person_name, person_id)
    _eids = element_ids or ""

    try:
        if _multi_pids_export:
            all_records: list = []
            seen_keys: set = set()
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(_multi_pids_export))) as ex:
                futs = {ex.submit(_fetch_all_pages, start_time, end_time, _eids, pid, None): pid
                        for pid in _multi_pids_export}
                for fut in concurrent.futures.as_completed(futs):
                    batch, _ = fut.result()
                    for r in batch:
                        k = _record_dedup_key(r)
                        if k not in seen_keys:
                            seen_keys.add(k)
                            all_records.append(r)
        else:
            all_records, _ = _fetch_all_pages(start_time, end_time, _eids, _pid, _pname)
    except Exception as e:
        raise HTTPException(500, str(e))

    decrypt_in_place(all_records, get_client().aes_key_hex)
    client = get_client()

    wb = Workbook()
    ws = wb.active
    ws.title = "Проходы"

    HDR_FILL = PatternFill("solid", fgColor="1E2235")
    HDR_FONT = Font(bold=True, color="C8CCDE", size=11)
    CENTER = Alignment(horizontal="center", vertical="center")
    MIDDLE = Alignment(vertical="center")
    PHOTO_W = 160
    PHOTO_H = 200
    ROW_H = 155  # pt ≈ 207px at 96dpi

    auth_map = {1: "Разрешён", 0: "Отказ"}
    zone_map = {"in": "В школе", "out": "Вне школы", "unknown": "—"}

    def _fmt_dt(s: str | None) -> str:
        if not s:
            return "—"
        try:
            return datetime.fromisoformat(s).strftime("%d.%m.%Y %H:%M:%S")
        except Exception:
            return s

    def _fmt_time(s: str | None) -> str:
        if not s:
            return "—"
        try:
            return datetime.fromisoformat(s).strftime("%H:%M:%S")
        except Exception:
            return s

    def _dept_short(full_path: str) -> str:
        parts = [p.strip() for p in full_path.split(">") if p.strip()]
        return parts[-1] if parts else full_path or "—"

    def _write_headers(headers: list[str], widths: list[int]) -> None:
        for ci, (h, w) in enumerate(zip(headers, widths), 1):
            cell = ws.cell(row=1, column=ci, value=h)
            cell.font = HDR_FONT
            cell.fill = HDR_FILL
            cell.alignment = CENTER
            ws.column_dimensions[cell.column_letter].width = w
        ws.freeze_panes = "A2"
        ws.row_dimensions[1].height = 22

    if grouped:
        groups = _group_by_person_py(all_records)

        # Параллельно тянем профильные фото
        photos: dict[int, bytes | None] = {}
        if with_photos:
            pids = [g["person_id"] for g in groups if g.get("person_id")]
            with concurrent.futures.ThreadPoolExecutor(max_workers=15) as ex:
                futs2 = {ex.submit(_fetch_photo_safe, client, person_id=pid): pid for pid in pids}
                for fut in concurrent.futures.as_completed(futs2):
                    photos[futs2[fut]] = fut.result()

        hdrs = ["#"]
        wdts = [5]
        if with_photos:
            hdrs.append("Фото"); wdts.append(25)
        hdrs += ["Имя", "ИИН", "Код", "Отдел / Класс", "Пришёл", "Последний проход", "Зона", "Проходов"]
        wdts += [32, 16, 12, 30, 12, 22, 14, 10]
        _write_headers(hdrs, wdts)

        for ri, g in enumerate(groups, 1):
            row = ri + 1
            ws.cell(row=row, column=1, value=ri).alignment = CENTER
            data_col = 2
            if with_photos:
                pid = g.get("person_id")
                if pid and photos.get(pid):
                    xl = _make_xl_image(photos[pid], PHOTO_W, PHOTO_H)
                    if xl:
                        ws.add_image(xl, ws.cell(row=row, column=2).coordinate)
                data_col = 3
            vals = [
                g["name"], g["iin"] or "—", g["code"] or "—",
                _dept_short(g["dept"]),
                _fmt_time(g["first_entry"]), _fmt_dt(g["last_pass"]),
                zone_map.get(g["zone"], "—"), g["pass_count"],
            ]
            for ci, v in enumerate(vals, data_col):
                ws.cell(row=row, column=ci, value=v).alignment = MIDDLE
            if with_photos:
                ws.row_dimensions[row].height = ROW_H

    else:
        # Период: одна строка = одна запись
        photos_period: dict[int, bytes | None] = {}
        if with_photos:
            tasks = {idx: (r.get("SnapPicUrl"), (r.get("Person") or {}).get("ID"))
                     for idx, r in enumerate(all_records)}
            with concurrent.futures.ThreadPoolExecutor(max_workers=15) as ex:
                futs2 = {ex.submit(_fetch_photo_safe, client, snap_url=snap, person_id=pid): idx
                         for idx, (snap, pid) in tasks.items()}
                for fut in concurrent.futures.as_completed(futs2):
                    photos_period[futs2[fut]] = fut.result()

        hdrs = ["#"]
        wdts = [5]
        if with_photos:
            hdrs.append("Фото"); wdts.append(25)
        hdrs += ["Имя", "ИИН", "Код", "Отдел / Класс", "Время", "Точка доступа", "Считыватель", "Результат"]
        wdts += [32, 16, 12, 30, 22, 22, 20, 12]
        _write_headers(hdrs, wdts)

        for idx, r in enumerate(all_records):
            row = idx + 2
            ri = idx + 1
            b = (r.get("Person") or {}).get("BaseInfo") or {}
            pid = (r.get("Person") or {}).get("ID")
            name = b.get("GivenName") or b.get("FullName") or (f"ID {pid}" if pid else "—")
            ws.cell(row=row, column=1, value=ri).alignment = CENTER
            data_col = 2
            if with_photos:
                pb = photos_period.get(idx)
                if pb:
                    xl = _make_xl_image(pb, PHOTO_W, PHOTO_H)
                    if xl:
                        ws.add_image(xl, ws.cell(row=row, column=2).coordinate)
                data_col = 3
                ws.row_dimensions[row].height = ROW_H
            vals = [
                name,
                b.get("FamilyName") or "—",
                b.get("PersonCode") or "—",
                _dept_short(b.get("FullPath") or ""),
                _fmt_dt(r.get("DeviceTime")),
                r.get("ElementName") or "—",
                r.get("CardReaderName") or "—",
                auth_map.get(r.get("SwipeAuthResult"), "—"),
            ]
            for ci, v in enumerate(vals, data_col):
                ws.cell(row=row, column=ci, value=v).alignment = MIDDLE

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"records_{(start_time or '')[:10]}_{(end_time or '')[:10]}.xlsx"
    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/api/access-points", tags=["Records"])
def list_access_points():
    """
    Список точек доступа.
    Приоритет: Elements API (с online-статусом). Fallback — сканирование записей за 30 дней.
    """
    def _elements_valid() -> bool:
        return (
            state._elements_cache["data"] is not None
            and time.time() - state._elements_cache["ts"] < state._ELEMENTS_CACHE_TTL_S
        )

    if _elements_valid():
        return {"access_points": state._elements_cache["data"]}

    with state._elements_lock:
        if _elements_valid():
            return {"access_points": state._elements_cache["data"]}

        try:
            raw = _hik_call(lambda c: c.logical_elements())
            el_list = _hik_data(raw).get("ElementList", {}).get("Element", [])
            if el_list:
                points = [
                    {
                        "id": str(e.get("ID", "")),
                        "guid": e.get("GUID", ""),
                        "name": e.get("Name", ""),
                        "type": e.get("Type"),
                        "online": bool(e.get("Online")),
                    }
                    for e in el_list
                ]
                points.sort(key=lambda x: x["name"])
                state._elements_cache["data"] = points
                state._elements_cache["ts"] = time.time()
                return {"access_points": points}
        except Exception:
            pass

    now = datetime.now(timezone.utc).astimezone()
    start = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    seen: dict[str, dict] = {}
    _p, _ps = 1, 500
    try:
        while True:
            raw = _hik_call(lambda c, __p=_p: c.card_swipe_records(
                page=__p, page_size=_ps,
                start_time=start.isoformat(timespec="seconds"),
                end_time=now.isoformat(timespec="seconds"),
            ))
            batch, total = _extract_records(raw)
            if not batch:
                break
            for rec in batch:
                name = str(rec.get("ElementName") or "")
                eid = str(rec.get("ElementID") or rec.get("DoorID") or "")
                key = eid if eid else name
                if name and key and key not in seen:
                    seen[key] = {"id": eid if eid else name, "name": name, "online": None}
            if len(seen) >= total or len(batch) < _ps:
                break
            _p += 1
    except Exception as e:
        return {"access_points": [], "error": str(e)}

    return {"access_points": sorted(seen.values(), key=lambda x: x["name"])}
