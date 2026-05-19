"""FastAPI прокси над HikCentral Professional."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import background  # noqa: F401 — starts session-watcher, event-poller, cache-prewarm threads
from routers import devices, events, persons, raw, records, session, stats, webhooks

tags_metadata = [
    {"name": "Session",  "description": "Логин и состояние сессии."},
    {"name": "Persons",  "description": "Список и детали учеников / пользователей."},
    {"name": "Records",  "description": "События доступа (проходы)."},
    {"name": "Stats",    "description": "Сводная статистика для дашборда."},
    {"name": "Devices",  "description": "Устройства, зоны, точки доступа и системная информация."},
    {"name": "Raw",      "description": "Сырой проксирующий вызов ISAPI."},
    {"name": "Webhooks", "description": "Подписка на события о новых проходах через HTTP POST."},
]

app = FastAPI(
    title="HikCentral Proxy",
    version="0.3.0",
    description=(
        "Прокси-API над HikCentral Professional V2.6 (ISAPI/Bumblebee). "
        "Автоматически добавляет AES-токены (`AppendInfo`) и расшифровывает "
        "ФИО / ИИН / телефон из ответов."
    ),
    openapi_tags=tags_metadata,
    docs_url="/docs",
    redoc_url="/redoc",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

for _router in [session.router, persons.router, records.router, stats.router, devices.router, events.router, raw.router, webhooks.router]:
    app.include_router(_router)


@app.get("/", include_in_schema=False)
def root():
    return {
        "name": "HikCentral Proxy",
        "version": app.version,
        "docs": "/docs",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
