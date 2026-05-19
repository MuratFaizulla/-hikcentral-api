from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import JSONResponse

import state
from core import _hik_call, _is_session_expired, _try_relogin, decrypt_in_place, get_client
from cache import _get_all_persons

router = APIRouter()


@router.get("/api/persons", tags=["Persons"])
def list_persons(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    search: str = Query("", description="Поиск по имени, коду или ИИН (12 цифр)"),
):
    """Список учеников с расшифрованными ФИО. Поиск по ИИН/коду — через кэш (расшифровка на стороне бэка)."""
    c = get_client()
    q = (search or "").strip().lower()
    if q:
        try:
            everyone = _get_all_persons(c)
        except Exception as e:
            raise HTTPException(500, str(e))

        def matches(p: dict) -> bool:
            b = p.get("BaseInfo", {}) or {}
            hay = " ".join([
                str(p.get("ID", "")),
                str(b.get("PersonCode", "")),
                str(b.get("FamilyName", "")),
                str(b.get("GivenName", "")),
                str(b.get("FullName", "")),
                str(b.get("Email", "")),
                str(b.get("PhoneNum", "")),
            ]).lower()
            return q in hay

        filtered = [p for p in everyone if matches(p)]
        total = len(filtered)
        start = (page - 1) * page_size
        return {
            "page": page,
            "page_size": page_size,
            "total": total,
            "persons": filtered[start: start + page_size],
        }

    try:
        raw = _hik_call(lambda c: c.list_persons(page=page, page_size=page_size, search=search))
    except Exception as e:
        raise HTTPException(500, str(e))

    rs = raw.get("ResponseStatus", {})
    if rs.get("ErrorCode", 0) != 0:
        return JSONResponse(status_code=400, content={"error": rs})

    pl = rs.get("Data", {}).get("PersonList", {})
    persons = pl.get("Person", [])
    decrypt_in_place(persons, get_client().aes_key_hex)
    return {
        "page": pl.get("PageIndex", page),
        "page_size": pl.get("PageSize", page_size),
        "total": pl.get("TotalNum") or pl.get("TotalCount") or pl.get("Total"),
        "persons": persons,
    }


@router.get("/api/persons/{person_id}", tags=["Persons"])
def get_person(person_id: int):
    """Детальная инфа по человеку."""
    try:
        raw = _hik_call(lambda c: c.get_person(person_id))
    except Exception as e:
        raise HTTPException(500, str(e))
    rs = raw.get("ResponseStatus", {})
    if rs.get("ErrorCode") != 0:
        return JSONResponse(status_code=400, content={"error": rs})
    data = rs.get("Data", {})
    decrypt_in_place(data, get_client().aes_key_hex)
    return data


@router.get(
    "/api/persons/{person_id}/photo",
    tags=["Persons"],
    responses={200: {"content": {"image/jpeg": {}}}},
)
def get_person_photo(person_id: int, photo_type: int = 0):
    """JPEG фото человека."""
    try:
        jpeg = get_client().get_photo(person_id, photo_type=photo_type)
    except Exception as e:
        raise HTTPException(500, str(e))
    if not jpeg.startswith(b'\xff\xd8'):
        try:
            err = __import__('json').loads(jpeg)
            rs = err.get("ResponseStatus", {})
            if _is_session_expired({"ResponseStatus": rs}) and _try_relogin():
                jpeg = get_client().get_photo(person_id, photo_type=photo_type)
            else:
                raise HTTPException(502, f"Hik error: {rs}")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, "invalid photo response")
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get(
    "/api/picture",
    tags=["Records"],
    responses={200: {"content": {"image/jpeg": {}}}},
)
def get_picture(url: str = Query(..., description="Vsm:// URL из поля SnapPicUrl записи")):
    """Снимок лица в момент прохода (Storage/Picture)."""
    c = get_client()
    try:
        data = c.get_picture(url)
    except Exception as e:
        raise HTTPException(500, str(e))
    if not data.startswith(b'\xff\xd8'):
        try:
            err = __import__('json').loads(data)
            rs = err.get("ResponseStatus", {})
            if _is_session_expired({"ResponseStatus": rs}) and _try_relogin():
                c = get_client()
                data = c.get_picture(url)
            else:
                raise HTTPException(502, f"Hik error: {rs}")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, "invalid picture response")
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )
