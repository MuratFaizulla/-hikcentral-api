from __future__ import annotations

import json
import time
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import StreamingResponse

import state
from core import _hik_call, get_client
from hik.crypto import decrypt_field

router = APIRouter()


def _hik_data(raw: dict) -> dict:
    rs = raw.get("ResponseStatus", raw)
    data = rs.get("Data", rs)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    return data if isinstance(data, dict) else {}


@router.get("/api/elements", tags=["Devices"])
def list_elements():
    """Список всех логических элементов (точек доступа) с online/offline статусом."""
    now_ts = time.time()
    with state._elements_lock:
        if (state._elements_cache["data"] is not None
                and now_ts - state._elements_cache["ts"] < state._ELEMENTS_CACHE_TTL_S):
            raw_elements = state._elements_cache["data"]
        else:
            try:
                raw = _hik_call(lambda c: c.logical_elements())
            except Exception as e:
                raise HTTPException(500, str(e))
            data = _hik_data(raw)
            el_list = data.get("ElementList", {}).get("Element", [])
            raw_elements = [
                {
                    "id": str(e.get("ID", "")),
                    "guid": e.get("GUID", ""),
                    "name": e.get("Name", ""),
                    "type": e.get("Type"),
                    "online": bool(e.get("Online")),
                }
                for e in (el_list or [])
            ]
            raw_elements.sort(key=lambda x: x["name"])
            state._elements_cache["data"] = raw_elements
            state._elements_cache["ts"] = now_ts

    total = len(raw_elements)
    online = sum(1 for e in raw_elements if e.get("online"))
    return {"total": total, "online": online, "offline": total - online, "elements": raw_elements}


@router.get("/api/areas", tags=["Devices"])
def list_areas():
    """Список логических зон (trk-in-*, trk-out-*, bas)."""
    now_ts = time.time()
    if state._areas_cache["data"] is not None and now_ts - state._areas_cache["ts"] < state._AREAS_CACHE_TTL_S:
        return {"areas": state._areas_cache["data"]}
    try:
        raw = _hik_call(lambda c: c.logical_areas())
    except Exception as e:
        raise HTTPException(500, str(e))
    area_list = _hik_data(raw).get("AreaList", {}).get("Area", [])
    areas = [
        {
            "id": str(a.get("ID", "")),
            "guid": a.get("GUID", ""),
            "name": a.get("Name", ""),
            "site_id": a.get("SiteID"),
        }
        for a in (area_list or [])
    ]
    areas.sort(key=lambda x: x["name"])
    state._areas_cache["data"] = areas
    state._areas_cache["ts"] = now_ts
    return {"total": len(areas), "areas": areas}


@router.get("/api/event-types", tags=["Devices"])
def list_event_types():
    """Типы событий ACS плагина (используются для фильтрации записей)."""
    now_ts = time.time()
    if (state._event_types_cache["data"] is not None
            and now_ts - state._event_types_cache["ts"] < state._EVENT_TYPES_CACHE_TTL_S):
        return state._event_types_cache["data"]
    try:
        raw = _hik_call(lambda c: c.acs_event_types())
    except Exception as e:
        raise HTTPException(500, str(e))
    state._event_types_cache["data"] = raw
    state._event_types_cache["ts"] = now_ts
    return raw


@router.get("/api/sites", tags=["Devices"])
def list_sites():
    """Информация о сервере HikCentral (версия, адрес, статус сети)."""
    try:
        raw = _hik_call(lambda c: c.sites())
    except Exception as e:
        raise HTTPException(500, str(e))
    site_list = _hik_data(raw).get("SiteList", {}).get("Site", [])
    sites = [
        {
            "id": s.get("ID"),
            "name": s.get("Name"),
            "address": s.get("Address") or s.get("IPAddress"),
            "version": s.get("SoftVersion") or s.get("ProtocolVersion"),
            "access_type": s.get("AccessType"),
            "network_ok": (s.get("Status") or {}).get("Network") == 1,
        }
        for s in (site_list if isinstance(site_list, list) else [])
    ]
    return {"sites": sites}


@router.get("/api/devices/{device_id}", tags=["Devices"])
def get_device(device_id: int):
    """Детали физического устройства по ID."""
    try:
        raw = _hik_call(lambda c: c.device_info(device_id))
    except Exception as e:
        raise HTTPException(500, str(e))
    rs = raw.get("ResponseStatus", {})
    if rs.get("ErrorCode", 0) != 0:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"error": rs})
    return rs.get("Data", raw)


@router.get("/api/devices/{device_id}/info", tags=["Devices"])
def get_device_info_v2(device_id: int):
    """Детали физического устройства (POST с пустым телом — как браузер)."""
    try:
        raw = _hik_call(lambda c: c.device_info_v2(device_id))
    except Exception as e:
        raise HTTPException(500, str(e))
    return raw


@router.get("/api/video/preview-url", tags=["Devices"])
def video_preview_url(extra_id: int = Query(100, description="ID элемента/канала"), site_id: int = Query(0)):
    """URL для live видео превью (WebSocket SMS). extra_id = ID из /api/video/hik-devices."""
    try:
        raw = _hik_call(lambda c: c.video_preview_url(extra_id=extra_id, site_id=site_id))
    except Exception as e:
        raise HTTPException(500, str(e))
    return raw


@router.get("/api/video/hik-devices", tags=["Devices"])
def list_hik_devices():
    """Список камер/элементов из HikCentral с IP-адресами (DeviceInfo)."""
    import re as _re
    devices = []
    c = get_client()
    try:
        raw = _hik_call(lambda c: c.video_elements())
        data = _hik_data(raw)
        el_list = data.get("ElementList", {}).get("Element") or []

        for e in (el_list if isinstance(el_list, list) else []):
            dev       = e.get("Device") or {}
            base_info = (dev.get("DeviceInfo") or {}).get("BaseInfo") or {}
            ip        = base_info.get("LoginAddress", "")
            http_port = base_info.get("HTTPPort") or 80
            rtsp_port = base_info.get("RTSPPort") or 554
            username  = base_info.get("UserName", "admin")
            serial    = base_info.get("SerialNumber", "")
            password_enc = base_info.get("Password", "")
            try:
                password = decrypt_field(password_enc, c.aes_key_hex) if password_enc else ""
            except Exception:
                password = ""
            model = ""
            if serial:
                m = _re.split(r'20\d{6}', serial)
                model = m[0] if len(m) > 1 else serial[:15]
            channel_id = (dev.get("ChannelInfo") or {}).get("ID") or e.get("ResourceID")
            devices.append({
                "id":         e.get("ID"),
                "channel_id": channel_id,
                "name":       e.get("Name", ""),
                "alias":      base_info.get("Alias", ""),
                "ip":         ip,
                "http_port":  int(http_port) if str(http_port).isdigit() else 80,
                "rtsp_port":  int(rtsp_port) if str(rtsp_port).isdigit() else 554,
                "username":   username,
                "password":   password,
                "model":      model,
                "serial":     serial,
                "online":     bool(e.get("Online")),
            })
    except Exception as e:
        raise HTTPException(500, str(e))

    return {"total": len(devices), "source": "video_elements", "devices": devices}


@router.get(
    "/api/video/snapshot/{channel_id}",
    tags=["Devices"],
    responses={200: {"content": {"image/jpeg": {}}}},
)
def video_snapshot(channel_id: int):
    """JPEG снимок канала через HikCentral. channel_id = ChannelInfo.ID из /api/video/hik-devices."""
    try:
        jpeg = get_client().capture_preview(channel_id)
    except Exception as e:
        raise HTTPException(502, f"Snapshot error: {e}")
    if not jpeg.startswith(b'\xff\xd8'):
        try:
            err = json.loads(jpeg)
            raise HTTPException(502, f"HikCentral: {err.get('ResponseStatus', err)}")
        except HTTPException:
            raise
        except Exception:
            pass
        raise HTTPException(502, f"Invalid snapshot ({len(jpeg)} bytes): {jpeg[:80]}")
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store"},
    )


async def _proxy_mjpeg(url: str, username: str, password: str) -> StreamingResponse:
    auth = httpx.DigestAuth(username, password) if username else None
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
        follow_redirects=True,
        auth=auth,
    )
    try:
        req = client.build_request("GET", url)
        resp = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        raise HTTPException(502, f"Камера недоступна: {e}")

    if resp.status_code == 401:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(401, f"401 Неверный логин/пароль для {url}")
    if resp.status_code == 404:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(404, f"404 MJPEG endpoint не найден: {url}")
    if resp.status_code != 200:
        body = await resp.aread()
        await client.aclose()
        raise HTTPException(resp.status_code, f"Камера вернула {resp.status_code}: {body[:200]}")

    ct = resp.headers.get("content-type", "multipart/x-mixed-replace; boundary=myboundary")

    async def _stream():
        try:
            async for chunk in resp.aiter_bytes(8192):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        _stream(),
        media_type=ct,
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/api/video/mjpeg", tags=["Devices"])
async def video_mjpeg_direct(
    ip: str = Query(..., description="IP-адрес камеры"),
    port: int = Query(80),
    channel: int = Query(1),
    username: str = Query("admin"),
    password: str = Query(""),
):
    """MJPEG поток напрямую с камеры (IP/credentials в query params)."""
    url = f"http://{ip}:{port}/ISAPI/Streaming/channels/{channel}01/httpPreview"
    return await _proxy_mjpeg(url, username, password)
