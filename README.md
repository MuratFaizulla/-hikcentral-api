# HikCentral Proxy — Backend

FastAPI-бэкенд, проксирующий HikCentral Professional v2.6 через ISAPI/Bumblebee.  
Автоматически подписывает запросы AES-токенами и расшифровывает зашифрованные поля (ФИО, ИИН, телефон).

---

## Содержание

- [Архитектура](#архитектура)
- [Запуск](#запуск)
- [Переменные окружения](#переменные-окружения)
- [Авторизация в HikCentral](#авторизация-в-hikcentral)
- [API — все эндпоинты](#api--все-эндпоинты)
- [Кэши и производительность](#кэши-и-производительность)
- [Фоновые потоки](#фоновые-потоки)
- [Шифрование](#шифрование)

---

## Архитектура

```
backend/
├── app.py                  # точка входа (~44 строки): FastAPI app + регистрация роутеров
├── state.py                # глобальное мутабельное состояние (клиент, локи, кэши)
├── core.py                 # _hik_call, decrypt_in_place, _resolve_person_query, get_client
├── cache.py                # кэширующие аксессоры с double-checked locking
├── background.py           # фоновые потоки (session-watcher, event-poller, cache-prewarm)
│
├── routers/                # FastAPI роутеры — по одному на домен
│   ├── __init__.py
│   ├── session.py          # /api/session, /api/login, /api/health, /api/cache/refresh
│   ├── persons.py          # /api/persons, /api/persons/{id}, /api/persons/{id}/photo, /api/picture
│   ├── records.py          # /api/records, /api/records/export.xlsx, /api/access-points
│   ├── stats.py            # /api/stats/today, /daily, /presence, /late
│   ├── devices.py          # /api/elements, /areas, /sites, /devices/*, /video/*
│   ├── events.py           # /api/events/stream  (SSE)
│   └── raw.py              # /api/raw  (сырой прокси ISAPI)
│
├── hik/                    # пакет: вся логика работы с HikCentral
│   ├── __init__.py         # re-export публичного API пакета
│   ├── client.py           # HTTP-клиент: токены, подпись, все ISAPI-вызовы
│   ├── crypto.py           # ne(), AppendInfo, AES decrypt, RC4Drop
│   ├── autologin.py        # Playwright headless-логин (захват SID + AES ключа)
│   └── direct_login.py     # прямой HTTP-логин (RSA + SHA256 деривация ключа)
│
├── session.json            # сохранённая сессия (SID + ключ + creds), gitignored
└── requirements.txt
```

### Правило импортов `hik/`

Внешний код всегда импортирует через пакет:
```python
from hik.client import HikClient, build_client_from_browser_capture
from hik.crypto import decrypt_field
from hik.autologin import capture_session_sync
from hik.direct_login import direct_login_or_playwright
# или через __init__.py:
from hik import HikClient, decrypt_field
```

### Правило: мутация глобального состояния

Все модули обращаются к состоянию через `import state; state.var = value`.  
Мутация через `from state import var; var = value` **не работает** (перепривязывает локальное имя).  
Мутация полей словаря (`state._cache["data"] = None`) работает из любого импорта.

---

## Запуск

### Docker (рекомендуется)

```bash
# из корня репозитория
docker compose up --build
```

Бэкенд будет доступен на `http://localhost:8000`.  
Swagger UI: `http://localhost:8000/docs`

### Локально

```bash
cd backend
pip install -r requirements.txt
playwright install chromium          # нужен только для автологина
uvicorn app:app --reload --port 8000
```

---

## Переменные окружения

| Переменная | По умолчанию | Описание |
|---|---|---|
| `HIK_BASE_URL` | `http://10.25.1.30` | URL HikCentral сервера |
| `HIK_SID` | — | SID сессии (альтернатива session.json) |
| `HIK_ENCRYPTED_AES_KEY` | — | зашифрованный AES ключ из localStorage |
| `HIK_HOSTNAME` | `10.25.1.30` | hostname для RC4Drop пасфразы |
| `HIK_USERNAME` | — | логин для авто-релогина |
| `HIK_PASSWORD` | — | пароль для авто-релогина |

Если `session.json` существует — переменные `HIK_SID` / `HIK_ENCRYPTED_AES_KEY` игнорируются.

---

## Авторизация в HikCentral

HikCentral использует самодельную схему токенов — обычный `Authorization: Bearer` не работает.

### Способ 1 — Автологин (рекомендуется)

```http
POST /api/login
Content-Type: application/json

{
  "base_url": "http://10.25.1.30",
  "username": "admin_trk",
  "password": "your_password"
}
```

Запускает headless Chromium, открывает страницу входа, захватывает SID и AES-ключ из `localStorage`, сохраняет в `session.json`. Все последующие запросы авторизуются автоматически.

### Способ 2 — Вручную из браузера

1. Зайти в HikCentral в браузере (Chrome/Firefox)
2. Открыть DevTools → Application → Local Storage → `80_pro_system_session_token`
3. Скопировать SID из Cookies (`SESSION_ID` или `JSESSIONID`)

```http
POST /api/session
Content-Type: application/json

{
  "sid": "<SID из куки>",
  "encrypted_aes_b64": "<значение из localStorage>",
  "base_url": "http://10.25.1.30"
}
```

### Авто-релогин

Session watcher проверяет сессию каждые 2 минуты через `KeepAlive`.  
При истечении (`ErrorCode` 200/216/220/222) — автоматически перелогинивается. Статус: `GET /api/health`.

```
session-watcher (фон, каждые 120s)
       │
       ▼
KeepAlive → ErrorCode = 0 ?
       │
       ├── да ──► всё хорошо, спать 120s
       │
       └── нет (200/216/220/222 = сессия истекла)
               │
               ▼
       _try_relogin()  ←  берёт creds из session.json или env
               │
               ▼
       direct_login_or_playwright()
               │
               ├─► direct_login  ~1s  ──► OK  →  новый SID + AES key
               │
               └─► Playwright    ~90s ──► OK  →  новый SID + AES key
                                                        │
                                                        ▼
                                               state._client = новый HikClient
                                               session.json обновлён
```

---

## API — все эндпоинты

Полная интерактивная документация: `http://localhost:8000/docs`

### Session

| Метод | Путь | Описание |
|---|---|---|
| `POST` | `/api/login` | Автологин через Playwright (сохраняет сессию) |
| `POST` | `/api/session` | Записать сессию вручную (SID + ключ из браузера) |
| `GET`  | `/api/session` | Текущий SID и AES-ключ |
| `GET`  | `/api/health` | Живость сессии + статус session watcher |
| `POST` | `/api/cache/refresh` | Сбросить все in-memory кэши |

### Persons

| Метод | Путь | Описание |
|---|---|---|
| `GET` | `/api/persons` | Список людей. `?search=` — поиск по ИИН/имени/коду (через кэш) |
| `GET` | `/api/persons/{id}` | Детали конкретного человека |
| `GET` | `/api/persons/{id}/photo` | JPEG фото |
| `GET` | `/api/picture?url=Vsm://...` | Снимок лица из SnapPicUrl записи прохода |

> HikCentral шифрует `FamilyName` (ИИН) и `GivenName` (имя). Бэкенд расшифровывает их прозрачно.  
> Поиск по ИИН работает через локальный кэш всех людей — HikCentral не умеет искать по зашифрованным полям.

### Records

| Метод | Путь | Описание |
|---|---|---|
| `GET` | `/api/records` | Проходы. `?fetch_all=true` — все страницы; `?person_name=ИИН` — поиск |
| `GET` | `/api/records/export.xlsx` | Выгрузка в Excel (те же фильтры) |
| `GET` | `/api/access-points` | Список точек доступа (с fallback из записей за 30 дней) |
| `GET` | `/api/events/stream` | SSE-поток: новые проходы в реальном времени (polling 10s) |

Параметры `/api/records`:

| Параметр | Тип | Описание |
|---|---|---|
| `page` / `page_size` | int | Пагинация |
| `fetch_all` | bool | Обойти все страницы HikCentral и вернуть одним списком |
| `start_time` / `end_time` | ISO 8601 | Период. По умолчанию — сегодня |
| `person_id` | int | Фильтр по ID человека |
| `person_name` | string | Поиск по имени или ИИН (через кэш) |
| `element_ids` | string | ID точек доступа через запятую |

### Stats

| Метод | Путь | Описание |
|---|---|---|
| `GET` | `/api/stats/today` | Кол-во людей в базе + кол-во проходов за сегодня |
| `GET` | `/api/stats/daily?days=7` | Количество проходов по дням (кэш 60s) |
| `GET` | `/api/stats/presence` | Кто сейчас в школе / ушёл / по классам (кэш 120s) |
| `GET` | `/api/stats/late?after=08:30` | Опоздавшие: пришли вовремя / с опозданием / не пришли |

Параметры `/api/stats/late`:

| Параметр | По умолчанию | Описание |
|---|---|---|
| `date` | сегодня | Дата в формате `YYYY-MM-DD` |
| `after` | `08:30` | Граница опоздания `HH:MM` |
| `element_ids` | все | Фильтр по точкам доступа (только вход) |

### Devices

| Метод | Путь | Описание |
|---|---|---|
| `GET` | `/api/elements` | Логические элементы (точки доступа) с online-статусом |
| `GET` | `/api/areas` | Логические зоны |
| `GET` | `/api/sites` | Информация о сервере HikCentral |
| `GET` | `/api/event-types` | Типы событий ACS плагина |
| `GET` | `/api/devices/{id}` | Детали физического устройства |
| `GET` | `/api/devices/{id}/info` | Расширенная инфа об устройстве |
| `GET` | `/api/video/hik-devices` | Камеры с IP, портами и расшифрованными паролями |
| `GET` | `/api/video/snapshot/{channel_id}` | JPEG снимок с канала |
| `GET` | `/api/video/mjpeg?ip=...` | MJPEG поток напрямую с камеры |
| `GET` | `/api/video/preview-url` | WebSocket URL для live превью |

### Raw

| Метод | Путь | Описание |
|---|---|---|
| `POST` | `/api/raw` | Сырой прокси-вызов любого ISAPI пути с авто-токеном |

```json
{ "path": "ISAPI/Bumblebee/Platform/V0/KeepLive", "mt": "GET", "body": null }
```

---

## Кэши и производительность

| Кэш | TTL | Инвалидируется при |
|---|---|---|
| Все люди (`_all_persons_cache`) | 300s | смене AES-ключа (ре-логин), `POST /api/cache/refresh` |
| Записи за сегодня (`_today_records_cache`) | 60s | новый проход в SSE поллере |
| Presence (`_presence_cache`) | 120s | новый проход в SSE поллере |
| Элементы (`_elements_cache`) | 60s | `POST /api/cache/refresh` |
| Зоны (`_areas_cache`) | 120s | — |
| Статистика по дням (`_stats_daily_cache`) | 60s | `POST /api/cache/refresh` |
| Типы событий (`_event_types_cache`) | 3600s | — |
| gid → dept (`_gid_dept_cache`) | 300s | обновляется из каждого прохода |

Все кэши используют **double-checked locking** — защита от race condition при параллельных запросах.

При старте контейнера фоновый поток `cache-prewarm` прогревает кэш людей и записей за сегодня (задержка 3s), убирая «холодный старт» первого запроса.

---

## Фоновые потоки

Запускаются при импорте `background.py` (т.е. при старте `app.py`):

**`session-watcher`** — каждые 120s вызывает `KeepAlive`. При `ErrorCode` истёкшей сессии запускает авто-релогин через `hik_direct_login`. При ошибке повторяет каждые 60s.

**`event-poller`** — каждые 10s запрашивает новые проходы (только если есть SSE-клиенты). Дедуплицирует по ключу `DeviceTime|ElementID|PersonID`. При новых записях инвалидирует кэши presence и today_records, рассылает SSE.

**`cache-prewarm`** — однократно через 3s после старта загружает всех людей и записи за сегодня.

---

## Внутреннее устройство пакета `hik/`

### hik/crypto.py — вся криптография

HikCentral не использует стандартные заголовки авторизации. Вместо этого — самодельная схема на основе AES и RC4.

**Как рождается `AppendInfo` для каждого запроса:**

```
tokenKeyNum = 3776
       │
       ▼
ne(3776) = 9          ← копия JS-функции: |sin|, |cos|, формула
       │
       ▼
plaintext = "3776:9"
       │
       ▼
AES-CBC(key=aes_key_hex, iv=000102030405060708090a0b0c0d0e0f, PKCS7)
       │
       ▼
AppendInfo = base64("dK3f+mNz…==")   →  уходит в заголовок каждого запроса
```

**Как расшифровываются поля персон (`decrypt_field`):**

```
FamilyName в ответе HikCentral = "dK3f+mNz…=="   (base64 blob)
       │
       ▼
base64_decode → bytes
       │
       ▼
AES-CBC decrypt(key=aes_key_hex, iv=000102…0e0f)   ← тот же ключ и IV
       │
       ▼
unpad PKCS7 → UTF-8
       │
       ▼
"030527600991"   (ИИН)
```

**Как из `localStorage` браузера получается `aes_key_hex` (`cryptojs_rc4drop_decrypt`):**

```
localStorage["80_pro_system_session_token"] = "U2FsdGVkX18xcXM0…"
       │
       ▼
base64_decode
       │
       ▼
"Salted__" (8) + salt (8) + ciphertext (N)
       │
       ▼
key = EVP_BytesToKey(MD5, passphrase="10.25.1.30", salt)   ← hostname как пасфраза
       │
       ▼
RC4Drop768(key, ciphertext)   ← RC4, но первые 192×4=768 байт keystream выброшены
       │
       ▼
"a3f1c8d2…"   (64-hex строка = AES-256 ключ)
```

---

### hik/client.py — HTTP-клиент

Единственный класс `HikClient`. Держит сессию и подписывает каждый запрос.

**Ключевая особенность ISAPI:** все вызовы физически — `POST`. Логический метод (`GET`/`POST`/`PUT`) передаётся параметром `?MT=GET`. Тело запроса — JSON, несмотря на `Content-Type: application/x-www-form-urlencoded` (так делает браузер, так делаем и мы).

Как выглядит каждый запрос к HikCentral:

```
Клиент (HikClient)                           HikCentral
   │                                              │
   │  [взять лок _lock]                           │
   │  n = _tkn;  _tkn += 1                        │
   │  token = base64( AES-CBC("n:ne(n)", key) )   │
   │  [освободить лок]                            │
   │                                              │
   │  POST /ISAPI/Bumblebee/ACSPlugin/...         │
   │       ?SID=8EFA5A4E…&MT=GET                  │
   │  AppendInfo: dK3f+mNz…==                     │
   │  body: {"CardSwipeRecordsRequest": {…}}      │
   │ ───────────────────────────────────────►     │
   │ ◄───────────────────────────────────────     │
   │  {"ResponseStatus": {"ErrorCode": 0,         │
   │    "Data": {"CardSwipeRecordsList": {…}}}}   │
```

Счётчик `_tkn` защищён `threading.Lock` — несколько параллельных FastAPI запросов не получат одинаковый номер токена.

**Специальные методы для бинарных ответов** (не используют `request_json`):

| Метод | Endpoint | Отличие |
|---|---|---|
| `get_picture(vsm_url)` | `Storage/Picture` | GET, Token в query |
| `get_photo(person_id)` | `PersonCredential/.../Photo` | GET, Token + time в query |
| `capture_preview(channel_id)` | `BaseVideo/CapturePreview` | POST, возвращает JPEG bytes |

**`build_client_from_browser_capture()`** — конструктор для ручного логина. Если `encrypted_aes_b64` начинается с `__hex__:` — это уже готовый ключ (от `direct_login`), иначе запускает RC4Drop расшифровку.

**`save_session` / `from_session_file`** — сохраняет `{sid, aes_key_hex, token_key_num, username, password}` в `session.json`. При перезапуске контейнера сессия восстанавливается без нового логина.

---

### hik/autologin.py — Playwright headless-логин

Запасной вариант логина — настоящий браузер (Chromium headless). Медленный (~90s), но работает даже если протокол логина изменится.

```
FastAPI handler                 Chromium headless           HikCentral
      │                               │                          │
      │  capture_session_sync()       │                          │
      │ ──────────────────────►       │                          │
      │   (новый поток + asyncio)     │                          │
      │                               │  GET /                   │
      │                               │ ────────────────────────►│
      │                               │ ◄────────────────────────│
      │                               │  страница логина         │
      │                               │                          │
      │                               │  fill username, password │
      │                               │  click "Вход"            │
      │                               │ ────────────────────────►│
      │                               │ ◄────────────────────────│
      │                               │  редирект, JS отрабатывает│
      │                               │                          │
      │                               │  poll localStorage       │
      │                               │  до 90s каждые 0.5s      │
      │                               │  80_pro_user.SID ✓       │
      │                               │  80_pro_system_session_token ✓
      │                               │                          │
      │ ◄──────────────────────       │                          │
      │  CapturedSession{             │                          │
      │    sid, encrypted_aes_b64,    │                          │
      │    tokenKeyNum }              │                          │
```

`capture_session_sync()` запускает `asyncio` в отдельном потоке (на Windows нужен `ProactorEventLoop` для поддержки subprocess Playwright).

**Когда используется:** `POST /api/login` → вызывается напрямую. При авто-релогине — только как fallback через `direct_login_or_playwright()`.

---

### hik/direct_login.py — прямой HTTP-логин (~1 сек)

Реверс-инженерия протокола логина HikCentral. Работает без браузера за 2 HTTP запроса.

```
Клиент                                       HikCentral
   │                                              │
   │  POST /Security/Crypto?MT=GET                │
   │  body: {}                                    │
   │ ───────────────────────────────────────►     │
   │ ◄───────────────────────────────────────     │
   │  { pre_SID: "ABC123…",                       │
   │    RSA_public_key: "<2048-bit PKCS#1 b64>" } │
   │                                              │
   │  [локально]                                  │
   │  enc_pass = RSA-PKCS1v15(rsa_key, password)  │
   │                                              │
   │  POST /Login?SID={pre_SID}&CT=0&MT=POST      │
   │  body: { UserName: "admin_trk",              │
   │          Password: "<RSA blob>",             │
   │          LoginAddress: "10.25.1.30" }        │
   │ ───────────────────────────────────────►     │
   │ ◄───────────────────────────────────────     │
   │  { SID: "8EFA5A4E…",                         │
   │    EncryInfo: { Challenge: "9AEF…",          │
   │                Iterations: 100 },            │
   │    tokenKeyNum: 3776 }                       │
   │                                              │
   │  [локально]                                  │
   │  aes_key = SHA256^100("password"+"9AEF…")    │
   │          = "a3f1c8…" (64-hex, AES-256)       │
   │                                              │
   │  Готово: SID + aes_key_hex + tokenKeyNum     │
```

AES ключ деривируется из пароля через SHA256 с итерациями — точная копия `createAESKey()` из `Common/common.js` HikCentral (обнаружено анализом JS). `tokenKeyNum` сдвигается на +50 чтобы не конфликтовать с параллельными сессиями браузера.

**`direct_login_or_playwright()`** — стратегия с fallback:

```
_try_relogin() в core.py
       │
       ▼
direct_login_or_playwright()
       │
       ├─► direct_login()  ──── ~1s ────► OK  →  return CapturedSession
       │                                  │
       │                               RuntimeError
       │                                  │
       └─► capture_session_sync()  ── ~90s ──► OK  →  return CapturedSession
               (Playwright)
```

---

## Шифрование

HikCentral шифрует чувствительные поля (ФИО, ИИН, телефон) алгоритмом **AES-128-ECB** с hex-ключом, получаемым через **RC4Drop** из строки `localStorage`.

Зашифрованные поля: `FamilyName` (ИИН), `GivenName` (имя), `FullName`, `MiddleName`, `PhoneNum`, `CertificateNo`, `Email`, `Address`, `Remark`, `CardNo`, `PassengerName`.

Все ответы бэкенда содержат уже расшифрованные значения — `decrypt_in_place()` в `core.py` рекурсивно обходит JSON и расшифровывает нужные поля на лету.

Каждый HTTP-запрос к HikCentral подписывается заголовком `AppendInfo` — токен строится из счётчика (`_tkn`) и AES-ключа. Счётчик монотонно растёт и защищает от replay-атак.
#   - h i k c e n t r a l - a p i  
 