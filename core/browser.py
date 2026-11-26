import asyncio
import json
import os
import random
from urllib.parse import unquote, urlparse
from patchright.async_api import Browser, BrowserContext, Locator, Page, Playwright
from config import get_storage_state_path


_ALLOWED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h", "socks4"}
_ACTIVE_PLAYWRIGHT_BY_STORE: dict[str, Playwright] = {}
_OWNED_PLAYWRIGHT_BY_STORE: dict[str, Playwright] = {}


def compute_cycle_sleep(
    cycle_elapsed: float, refresh_interval: float, min_gap: float = 3.0
) -> float:
    """Return sleep duration targeting refresh_interval cadence with a bounded safety floor."""
    interval = max(1.0, float(refresh_interval))
    floor = min(float(min_gap), interval)
    return max(floor, interval - max(0.0, float(cycle_elapsed)))


def is_target_closed_error(exc: BaseException) -> bool:
    """Return True when an exception indicates a dead/closed Chromium page, context, or browser."""
    msg = str(exc).lower()
    return any(
        tok in msg
        for tok in (
            "target page, context or browser has been closed",
            "browser has been closed",
            "context or browser has been closed",
            "target closed",
            "connection closed",
            "epipe",
        )
    )


def get_proxy_config() -> dict | None:
    """Parse HOME_PROXY_URL env var into a Playwright proxy dict, or None if unset."""
    raw = os.getenv("HOME_PROXY_URL", "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_PROXY_SCHEMES or not parsed.hostname:
        print("[UYARI] HOME_PROXY_URL geçersiz formatta; proxy devre dışı bırakıldı.")
        return None
    try:
        port = parsed.port
    except ValueError:
        print("[UYARI] HOME_PROXY_URL port değeri geçersiz; proxy devre dışı bırakıldı.")
        return None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    server = f"{scheme}://{host}:{port}" if port is not None else f"{scheme}://{host}"
    config: dict = {"server": server}
    if parsed.username:
        config["username"] = unquote(parsed.username)
    if parsed.password:
        config["password"] = unquote(parsed.password)
    return config


def _mask_proxy_server(server: str) -> str:
    """Mask proxy hostname/IP so public GitHub Actions logs never leak Tailscale addresses."""
    parsed = urlparse(server)
    host = parsed.hostname or ""
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        masked_host = f"{parts[0]}.x.x.{parts[3]}"
    elif ":" in host:
        first_seg = host.split(":", 1)[0] or "ip6"
        masked_host = f"[{first_seg}::***]"
    elif host:
        masked_host = f"{host[:2]}***" if len(host) > 2 else "***"
    else:
        masked_host = "***"
    try:
        port = parsed.port
    except ValueError:
        port = None
    port_part = f":{port}" if port is not None else ""
    scheme = (parsed.scheme or "http").lower()
    return f"{scheme}://{masked_host}{port_part}"


def _load_valid_storage_state(state_path: str) -> str | None:
    """Return state_path if it exists and contains valid Playwright storage JSON."""
    if not os.path.exists(state_path) or os.path.getsize(state_path) == 0:
        return None
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if (
            isinstance(data, dict)
            and isinstance(data.get("cookies"), list)
            and ("origins" not in data or isinstance(data["origins"], list))
        ):
            return state_path
    except Exception as exc:
        print(f"[UYARI] Geçersiz oturum dosyası ({state_path}), sıfırdan başlanacak: {exc}")
    return None


async def launch_store_browser(
    playwright: Playwright, store_name: str
) -> tuple[Browser, BrowserContext]:
    """Launch headed Chromium (for Xvfb) with tr-TR locale and optional masked home proxy."""
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

    _ACTIVE_PLAYWRIGHT_BY_STORE[store_name.lower()] = playwright
    browser = await playwright.chromium.launch(**launch_kwargs)

    state_path = get_storage_state_path(store_name)
    context_kwargs: dict = {
        "viewport": {"width": 1920, "height": 1080},
        "locale": "tr-TR",
        "timezone_id": "Europe/Istanbul",
    }

    # Ev proxy'si varsa tarayıcı bağlamına enjekte et (Zara/Mango için)
    proxy_cfg = get_proxy_config()
    if proxy_cfg:
        context_kwargs["proxy"] = proxy_cfg
        auth_label = "kimlik doğrulamalı" if proxy_cfg.get("username") else "kimliksiz"
        print(
            f"[{store_name.upper()}] Ev proxy'si aktif: "
            f"{_mask_proxy_server(proxy_cfg['server'])} ({auth_label})"
        )

    valid_state = _load_valid_storage_state(state_path)
    if valid_state:
        context_kwargs["storage_state"] = valid_state
        print(f"[{store_name.upper()}] Kayıtlı oturum dosyası yüklendi: {valid_state}")

    context = await browser.new_context(**context_kwargs)
    return browser, context


async def close_browser_safely(
    context: BrowserContext | None = None,
    browser: Browser | None = None,
    timeout_sec: float = 8.0,
) -> None:
    """Close a Playwright context and browser with strict timeouts so hung sockets never deadlock."""
    resolved_browser = browser
    if resolved_browser is None and context is not None:
        try:
            resolved_browser = getattr(context, "browser", None)
        except Exception:
            resolved_browser = None

    if context is not None:
        try:
            await asyncio.wait_for(context.close(), timeout=timeout_sec)
        except Exception:
            pass

    if resolved_browser is not None:
        try:
            await asyncio.wait_for(resolved_browser.close(), timeout=timeout_sec)
        except Exception:
            pass


async def cleanup_owned_playwright(store_name: str, timeout_sec: float = 8.0) -> None:
    """Stop any fallback Playwright driver spawned during live browser recovery."""
    key = store_name.lower()
    owned = _OWNED_PLAYWRIGHT_BY_STORE.pop(key, None)
    if owned is not None:
        try:
            await asyncio.wait_for(owned.stop(), timeout=timeout_sec)
        except Exception:
            pass


async def relaunch_store_browser(
    store_name: str,
    old_context: BrowserContext | None = None,
    old_browser: Browser | None = None,
    playwright: Playwright | None = None,
) -> tuple[Browser, BrowserContext]:
    """Tear down a hung/closed browser+context and launch a fresh one with saved storage_state."""
    key = store_name.lower()
    await close_browser_safely(context=old_context, browser=old_browser)

    pw = playwright or _ACTIVE_PLAYWRIGHT_BY_STORE.get(key)
    if pw is None:
        from patchright.async_api import async_playwright

        await cleanup_owned_playwright(key)
        pw = await async_playwright().start()
        _OWNED_PLAYWRIGHT_BY_STORE[key] = pw
        _ACTIVE_PLAYWRIGHT_BY_STORE[key] = pw

    print(f"[{store_name.upper()}] Tarayıcı ve bağlam kayıtlı oturumla canlı yeniden başlatılıyor...")
    return await launch_store_browser(pw, store_name)


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
