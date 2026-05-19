"""
Автологин в HikCentral через Playwright (headless Chromium).

Заходит на /, заполняет форму, ждёт пока login response отработает
и localStorage наполнится, потом возвращает SID + encrypted AES key.

Используется одноразово при старте контейнера или по запросу /api/login,
чтобы не править session.json вручную.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CapturedSession:
    sid: str
    encrypted_aes_b64: str
    token_key_num: int
    hostname: str
    base_url: str


async def capture_session(
    base_url: str,
    username: str,
    password: str,
    hostname: Optional[str] = None,
    timeout_s: int = 90,
) -> CapturedSession:
    """
    Логинится в HikCentral headless‑браузером и возвращает credentials.
    Использует Playwright Chromium.
    """
    from playwright.async_api import async_playwright

    if hostname is None:
        from urllib.parse import urlparse
        hostname = urlparse(base_url).hostname or "10.25.1.30"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            ctx = await browser.new_context(
                ignore_https_errors=True,
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                ),
            )
            page = await ctx.new_page()

            logger.info("Goto %s", base_url)
            await page.goto(base_url, wait_until="load", timeout=timeout_s * 1000)

            pass_input = page.locator('input[type="password"]').first
            await pass_input.wait_for(timeout=timeout_s * 1000)

            user_input = page.locator('input[type="text"]:not([readonly])').first
            try:
                await user_input.wait_for(state="visible", timeout=3000)
            except Exception:
                user_input = page.locator('input[type="text"]').nth(1)

            await user_input.fill(username)
            await pass_input.fill(password)

            buttons = page.locator("button")
            count = await buttons.count()
            clicked = False
            for i in range(count):
                try:
                    txt = (await buttons.nth(i).inner_text(timeout=1000)).strip()
                except Exception:
                    continue
                if any(k in txt for k in ("Вход", "Login", "Sign in", "登录")):
                    await buttons.nth(i).click()
                    clicked = True
                    break
            if not clicked:
                await pass_input.press("Enter")

            async def has_session() -> bool:
                data = await page.evaluate(
                    "() => ({user: localStorage.getItem('80_pro_user'), aes: localStorage.getItem('80_pro_system_session_token')})"
                )
                return bool(data.get("user") and data.get("aes"))

            deadline = asyncio.get_event_loop().time() + timeout_s
            while asyncio.get_event_loop().time() < deadline:
                if await has_session():
                    break
                err = await page.evaluate(
                    "() => document.body && document.body.innerText.includes('CAPTCHA') ? 'captcha' : ''"
                )
                if err == "captcha":
                    raise RuntimeError("CAPTCHA required — пройди логин руками один раз")
                await asyncio.sleep(0.5)
            else:
                raise TimeoutError("login: localStorage не заполнился за %ds" % timeout_s)

            data = await page.evaluate(
                """() => {
                    const u = JSON.parse(localStorage.getItem('80_pro_user') || '{}');
                    return {
                        sid: u.SID,
                        enc: localStorage.getItem('80_pro_system_session_token'),
                        tkn: parseInt(localStorage.getItem('80_pro_tokenKeyNum') || '11', 10)
                    };
                }"""
            )

            if not data.get("sid") or not data.get("enc"):
                raise RuntimeError(f"login: пустые credentials — {data}")

            return CapturedSession(
                sid=data["sid"],
                encrypted_aes_b64=data["enc"],
                token_key_num=max(int(data["tkn"]) + 50, 100),
                hostname=hostname,
                base_url=base_url,
            )
        finally:
            await browser.close()


def capture_session_sync(*args, **kwargs) -> CapturedSession:
    """Синхронная обёртка для использования из FastAPI handler."""
    import threading
    result = [None]
    error = [None]

    def _run():
        import sys
        if sys.platform == 'win32':
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result[0] = loop.run_until_complete(capture_session(*args, **kwargs))
        except Exception as e:
            error[0] = e
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join()
    if error[0] is not None:
        raise error[0]
    return result[0]
