"""
Прямой HTTP-логин в HikCentral без Playwright.

Протокол (обнаружен анализом JS-кода HikCentral 2.6):
  1. POST /ISAPI/Bumblebee/Platform/V0/Security/Crypto?MT=GET
     → получаем SID + RSA-2048 public key (PKCS#1 DER, base64)
  2. RSA-PKCS1v15 шифруем пароль открытым ключом
  3. POST /ISAPI/Bumblebee/Platform/V0/Login?SID={SID}&CT=0&MT=POST
     body: {"LoginRequest": {"UserName": "...", "Password": "<base64>",
                              "LoginAddress": "<ip>", "LoginModel": 1,
                              "IsRSMWebLogin": 0}}
  4. Из успешного ответа берём SID и AES-related поля

Возвращает CapturedSession (тот же тип что hik/autologin.py).
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from hik.autologin import CapturedSession

logger = logging.getLogger(__name__)


def _rsa_encrypt_pkcs1v15(public_key_b64: str, plaintext: str) -> str:
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import PKCS1_v1_5
    key = RSA.import_key(base64.b64decode(public_key_b64))
    cipher = PKCS1_v1_5.new(key)
    ct = cipher.encrypt(plaintext.encode("utf-8"))
    return base64.b64encode(ct).decode("ascii")


def _get_nested(obj: Any, path: str) -> Any:
    if not path:
        return None
    parts = path.replace("[", ".[").split(".")
    cur = obj
    for part in parts:
        if cur is None:
            return None
        if part.startswith("[") and part.endswith("]"):
            try:
                cur = cur[int(part[1:-1])]
            except (IndexError, TypeError, ValueError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _try_paths(obj: Any, *paths: str) -> Any:
    for p in paths:
        v = _get_nested(obj, p)
        if v is not None:
            return v
    return None


_SID_PATHS = [
    "ResponseStatus.Data.Login.SID",
    "ResponseStatus.Data.SID",
    "Data.Login.SID",
    "Data.SID",
    "SID",
]

_AES_ENC_PATHS = [
    "ResponseStatus.Data.Login.SystemSessionToken",
    "ResponseStatus.Data.Login.AesToken",
    "ResponseStatus.Data.Login.EncryptedKey",
    "Data.Login.SystemSessionToken",
    "Data.Login.Token",
    "SystemSessionToken",
]

_AES_SOURCE_PATHS = [
    "ResponseStatus.Data.Login.AesSourceKey",
    "Data.Login.AesSourceKey",
    "AesSourceKey",
]
_CHALLENGE_PATHS = [
    "ResponseStatus.Data.Login.Challenge",
    "Data.Login.Challenge",
    "Challenge",
]
_ITERATIONS_PATHS = [
    "ResponseStatus.Data.Login.Iterations",
    "Data.Login.Iterations",
    "Iterations",
]
_TOKENNUM_PATHS = [
    "ResponseStatus.Data.Login.tokenKeyNum",
    "ResponseStatus.Data.Login.TokenKeyNum",
    "Data.Login.tokenKeyNum",
    "tokenKeyNum",
]


def create_aes_key(password: str, challenge: str, iterations: int) -> str:
    """SHA256^iterations(password + challenge) — точная копия HikCentral JS createAESKey."""
    import hashlib
    h = hashlib.sha256((password + challenge).encode("utf-8")).hexdigest()
    for _ in range(1, iterations):
        h = hashlib.sha256(h.encode("utf-8")).hexdigest()
    return h


def direct_login(
    base_url: str,
    username: str,
    password: str,
    hostname: Optional[str] = None,
    timeout: float = 30.0,
) -> CapturedSession:
    """Прямой HTTP-логин в HikCentral без Playwright. Raises RuntimeError если не прошёл."""
    base_url = base_url.rstrip("/")
    if hostname is None:
        hostname = urlparse(base_url).hostname or base_url

    client = httpx.Client(
        base_url=base_url,
        verify=False,
        timeout=timeout,
        follow_redirects=True,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, */*",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        },
    )

    with client:
        logger.info("direct_login: GET /Security/Crypto …")
        r = client.post("/ISAPI/Bumblebee/Platform/V0/Security/Crypto?MT=GET", json={})
        r.raise_for_status()
        crypto_data = r.json()

        err_code = crypto_data.get("ResponseStatus", {}).get("ErrorCode")
        if err_code not in (None, 0):
            raise RuntimeError(f"Crypto endpoint вернул ErrorCode={err_code}")

        crypto_resp = crypto_data["ResponseStatus"]["Data"]["CryptoResponse"]
        pre_sid = crypto_resp["SID"]
        rsa_key_b64 = crypto_resp["CryptoKey"]
        logger.info("direct_login: pre-SID=%s…", pre_sid[:8])

        enc_password = _rsa_encrypt_pkcs1v15(rsa_key_b64, password)

        login_body = {
            "LoginRequest": {
                "UserName": username,
                "Password": enc_password,
                "LoginAddress": hostname,
                "LoginModel": 1,
                "IsRSMWebLogin": 0,
            }
        }
        logger.info("direct_login: POST /Login …")
        r2 = client.post(
            f"/ISAPI/Bumblebee/Platform/V0/Login?SID={pre_sid}&CT=0&MT=POST",
            json=login_body,
        )
        r2.raise_for_status()
        login_data = r2.json()

        resp_status = login_data.get("ResponseStatus", {})
        login_err = resp_status.get("ErrorCode")
        if login_err != 0:
            err_msg = resp_status.get("ErrorMsg", "")
            remaining = (
                login_data.get("ResponseStatus", {})
                .get("Data", {})
                .get("Login", {})
                .get("RemainingLoginNumber")
            )
            detail = f"ErrorCode={login_err}"
            if err_msg:
                detail += f" ({err_msg})"
            if remaining is not None:
                detail += f", осталось попыток: {remaining}"
            raise RuntimeError(f"Login failed: {detail}")

        login_resp_data = resp_status.get("Data", {})

        sid = _try_paths(login_resp_data, *["Login.SID", "SID", "Login.SessionId", "SessionId"])
        if not sid:
            sid = _try_paths(login_data, *_SID_PATHS)
        if not sid:
            raise RuntimeError(
                f"SID не найден в login response. Keys: {list(login_resp_data.keys())}"
            )

        enc_info = _try_paths(login_resp_data, "Login.EncryInfo") or {}
        challenge_val = enc_info.get("Challenge") or _try_paths(
            login_resp_data, "Login.Challenge", "Challenge"
        )
        iterations_val = enc_info.get("Iterations") or _try_paths(
            login_resp_data, "Login.Iterations", "Iterations"
        ) or 100

        if not challenge_val:
            raise RuntimeError(
                f"EncryInfo.Challenge не найден в login response.\n"
                f"Data: {json.dumps(login_resp_data, ensure_ascii=False)[:600]}"
            )

        aes_key_hex: str = create_aes_key(password, challenge_val, int(iterations_val))
        logger.info("direct_login: AES key derived: %s…", aes_key_hex[:16])

        tkn = _try_paths(login_resp_data, *[
            "Login.tokenKeyNum", "Login.TokenKeyNum", "tokenKeyNum",
        ])
        try:
            tkn_int = int(tkn or 11)
        except (TypeError, ValueError):
            tkn_int = 11

        logger.info("direct_login: успех! SID=%s… tkn=%d", sid[:8], tkn_int)

        return CapturedSession(
            sid=sid,
            encrypted_aes_b64=f"__hex__:{aes_key_hex}",
            token_key_num=max(tkn_int + 50, 100),
            hostname=hostname,
            base_url=base_url,
        )


def direct_login_or_playwright(
    base_url: str,
    username: str,
    password: str,
    hostname: Optional[str] = None,
    timeout_s: int = 90,
) -> CapturedSession:
    """Сначала пробует прямой HTTP-логин (~1 сек), при неудаче — Playwright (~90 сек)."""
    try:
        logger.info("Пробую прямой HTTP-логин…")
        sess = direct_login(base_url, username, password, hostname)
        logger.info("Прямой HTTP-логин прошёл успешно (SID=%s…)", sess.sid[:8])
        return sess
    except Exception as e:
        logger.warning("Прямой HTTP-логин не удался: %s. Запускаю Playwright…", e)

    from hik.autologin import capture_session_sync
    return capture_session_sync(
        base_url=base_url,
        username=username,
        password=password,
        hostname=hostname,
        timeout_s=timeout_s,
    )
