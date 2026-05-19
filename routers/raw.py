from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core import get_client

router = APIRouter()


class RawRequest(BaseModel):
    path: str = Field(..., description="ISAPI путь, e.g. `ISAPI/Bumblebee/Platform/V0/KeepLive`")
    mt: str = Field("GET", description="Логический метод HikCentral (GET/POST/PUT/DELETE)")
    body: Optional[Any] = Field(None, description="Тело запроса (JSON-сериализуется)")


@router.post("/api/raw", tags=["Raw"])
def raw_request(payload: RawRequest):
    """Сырой прокси-вызов ISAPI с авто-токеном."""
    c = get_client()
    r = c.request(payload.path, mt=payload.mt, body=payload.body)
    try:
        return r.json()
    except Exception:
        return {"status": r.status_code, "text": r.text}
