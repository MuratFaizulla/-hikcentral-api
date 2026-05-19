"""Webhooks: регистрация URL и доставка событий о новых проходах."""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

WEBHOOKS_FILE = Path(__file__).parent.parent / "webhooks.json"
_lock = threading.Lock()

router = APIRouter()


# ── Storage ────────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    try:
        return json.loads(WEBHOOKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(hooks: list[dict]) -> None:
    WEBHOOKS_FILE.write_text(
        json.dumps(hooks, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def has_active_webhooks() -> bool:
    return bool(_load())


# ── HTTP delivery ──────────────────────────────────────────────────────────

def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _deliver(hook: dict, payload: dict) -> tuple[bool, int, str]:
    """POST payload на hook URL. Возвращает (ok, http_status, error_msg)."""
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Hik-Event": payload.get("event", "new_passes"),
        "User-Agent": "HikCentral-Proxy/1.0",
    }
    if hook.get("secret"):
        headers["X-Hik-Signature"] = _sign(hook["secret"], body)
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(hook["url"], content=body, headers=headers)
        return resp.is_success, resp.status_code, ""
    except Exception as e:
        return False, 0, str(e)


def fire_webhooks(records: list[dict]) -> None:
    """Вызывается из event-poller. Fire-and-forget в daemon-потоке."""
    hooks = _load()
    if not hooks:
        return

    payload = {
        "event": "new_passes",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "records": records,
    }

    def _send_all() -> None:
        for hook in hooks:
            ok, _, _ = _deliver(hook, payload)
            if not ok:
                time.sleep(2)
                _deliver(hook, payload)  # 1 повтор

    threading.Thread(target=_send_all, daemon=True, name="webhook-fire").start()


# ── API endpoints ──────────────────────────────────────────────────────────

class WebhookIn(BaseModel):
    url: str
    secret: Optional[str] = None


@router.post("/api/webhooks", tags=["Webhooks"], status_code=201)
def create_webhook(body: WebhookIn):
    """
    Зарегистрировать URL для получения уведомлений о новых проходах.

    При каждом новом проходе бэкенд отправит POST на этот URL:
    ```json
    {
      "event": "new_passes",
      "timestamp": "2026-05-19T10:00:00",
      "records": [{ ... }]
    }
    ```
    Если указан `secret` — заголовок `X-Hik-Signature: sha256=<hmac>` позволяет
    проверить подлинность запроса на принимающей стороне.
    """
    with _lock:
        hooks = _load()
        if any(h["url"] == body.url for h in hooks):
            raise HTTPException(400, "URL уже зарегистрирован")
        hook = {
            "id": str(uuid.uuid4()),
            "url": body.url,
            "secret": body.secret,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        hooks.append(hook)
        _save(hooks)
    return {"id": hook["id"], "url": hook["url"], "created_at": hook["created_at"]}


@router.get("/api/webhooks", tags=["Webhooks"])
def list_webhooks():
    """Список зарегистрированных webhooks (secret не возвращается)."""
    hooks = _load()
    return {
        "webhooks": [
            {"id": h["id"], "url": h["url"], "created_at": h["created_at"]}
            for h in hooks
        ]
    }


@router.delete("/api/webhooks/{webhook_id}", tags=["Webhooks"])
def delete_webhook(webhook_id: str):
    """Удалить webhook по ID."""
    with _lock:
        hooks = _load()
        new_hooks = [h for h in hooks if h["id"] != webhook_id]
        if len(new_hooks) == len(hooks):
            raise HTTPException(404, "Webhook не найден")
        _save(new_hooks)
    return {"deleted": webhook_id}


@router.post("/api/webhooks/{webhook_id}/test", tags=["Webhooks"])
def test_webhook(webhook_id: str):
    """
    Отправить тестовый запрос на webhook URL.
    Полезно чтобы убедиться что URL доступен и принимает данные.
    """
    hooks = _load()
    hook = next((h for h in hooks if h["id"] == webhook_id), None)
    if not hook:
        raise HTTPException(404, "Webhook не найден")

    payload = {
        "event": "test",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "records": [],
    }
    ok, status, error = _deliver(hook, payload)
    if ok:
        return {"status": "ok", "http_status": status}
    raise HTTPException(502, f"Доставка не удалась: {error}")
