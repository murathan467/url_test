import asyncio
import json
import os
import random
from patchright.async_api import Browser, BrowserContext, Locator, Page, Playwright, Route
from config import get_storage_state_path


async def _block_heavy_resources(route: Route) -> None:
    """Block images, media, and fonts so many concurrent tabs stay lightweight."""
    try:
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.continue_()
    except Exception:
        # Ignore benign errors when a tab closes mid-request.
        pass


def _load_valid_storage_state(state_path: str) -> str | None:
    """Return state_path if it exists and contains valid Playwright storage JSON."""
    if not os.path.exists(state_path) or os.path.getsize(state_path) == 0:
        return None
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "cookies" in data:
            return state_path
    except Exception as exc:
        print(f"[UYARI] Geçersiz oturum dosyası ({state_path}), sıfırdan başlanacak: {exc}")
    return None


async def launch_store_browser(
    playwright: Playwright, store_name: str
) -> tuple[Browser, BrowserContext]:
    """Launch headed Chromium (for Xvfb) with tr-TR locale and resource blocking."""
    launch_kwargs: dict = {
        "headless": False,
        "args": [
            "--window-size=1920,1080",
            "--start-maximized",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    }
    if os.name == "nt":
        launch_kwargs["channel"] = "chrome"

    browser = await playwright.chromium.launch(**launch_kwargs)

    state_path = get_storage_state_path(store_name)
    context_kwargs: dict = {
        "viewport": {"width": 1920, "height": 1080},
        "locale": "tr-TR",
        "timezone_id": "Europe/Istanbul",
    }

    valid_state = _load_valid_storage_state(state_path)
    if valid_state:
        context_kwargs["storage_state"] = valid_state
        print(f"[{store_name.upper()}] Kayıtlı oturum dosyası yüklendi: {valid_state}")

    context = await browser.new_context(**context_kwargs)
    await context.route("**/*", _block_heavy_resources)
    return browser, context


async def click_first_visible(
    page: Page, selectors: list[str], timeout_ms: int = 2000, post_wait_ms: int = 1000
) -> str | None:
    """Click the first visible element matching any selector and return the matched selector."""
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=timeout_ms):
                await btn.click()
                if post_wait_ms > 0:
                    await page.wait_for_timeout(post_wait_ms)
                return sel
        except Exception:
            continue
    return None


async def safe_goto(
    page: Page, url: str, timeout_ms: int = 30000, retries: int = 2
) -> int | None:
    """Navigate to url with bounded retries on transient connection errors."""
    last_exc: Exception | None = None
    attempts = max(1, retries)
    for attempt in range(1, attempts + 1):
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return resp.status if resp else None
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                print(
                    f"[UYARI] Sayfa yükleme hatası ({url}, deneme {attempt}/{attempts}): {exc}. "
                    "2 sn sonra yeniden deneniyor..."
                )
                await asyncio.sleep(2)
    assert last_exc is not None
    raise last_exc


async def safe_reload_or_goto(
    page: Page, fallback_url: str, timeout_ms: int = 30000
) -> bool:
    """Reload the active page, falling back to fallback_url on connection or chrome-error states."""
    current_url = (page.url or "").lower()
    if current_url.startswith("chrome-error://") or current_url.startswith("about:blank") or not current_url:
        try:
            await safe_goto(page, fallback_url, timeout_ms=timeout_ms, retries=2)
            return True
        except Exception as exc:
            print(f"[HATA] Yedek sayfa yüklemesi başarısız ({fallback_url}): {exc}")
            return False

    try:
        await page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
        return True
    except Exception as exc:
        print(
            f"[UYARI] Sayfa yenileme başarısız ({exc}); 2 sn sonra {fallback_url} adresine gidiliyor..."
        )
        await asyncio.sleep(2)
        try:
            await safe_goto(page, fallback_url, timeout_ms=timeout_ms, retries=2)
            return True
        except Exception as fallback_exc:
            print(f"[HATA] Sayfa kurtarılamadı ({fallback_url}): {fallback_exc}")
            return False


async def page_diagnostics(page: Page) -> str:
    """Return a concise, secret-free summary of the current page URL, title, and body snippet."""
    url = page.url or ""
    title = ""
    sample = ""
    try:
        title = (await page.title() or "").strip()
    except Exception:
        pass
    try:
        raw_text = await page.evaluate(
            "() => (document.body && document.body.innerText ? document.body.innerText : '')"
        )
        sample = " ".join(str(raw_text).split())[:160]
    except Exception:
        pass
    return f"url={url} | title={title} | ozet={sample}"


async def human_type(locator: Locator, text: str) -> None:
    """Type text sequentially with human-like randomized keystroke delays."""
    await locator.click()
    await locator.fill("")
    await locator.press_sequentially(text, delay=random.randint(45, 105))


async def scroll_page_to_bottom(
    page: Page, max_scrolls: int = 8, wait_ms: int = 1200
) -> None:
    """Scroll down incrementally so lazy-loaded wishlist items render."""
    try:
        last_height = await page.evaluate("document.body.scrollHeight")
        for _ in range(max_scrolls):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(wait_ms)
            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
