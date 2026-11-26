import asyncio
import json
import time
from collections.abc import Callable
from patchright.async_api import BrowserContext, Page, Response
from config import (
    COMMON_EMAIL,
    COMMON_PASSWORD,
    ERROR_COOLDOWN,
    MANGO_FAVORITES_URL,
    MANGO_LOGIN_URL,
    REFRESH_INTERVAL,
    TARGET_SIZES,
)
from core.browser import (
    click_first_visible,
    human_type,
    page_diagnostics,
    safe_goto,
    safe_reload_or_goto,
)
from core.notifier import StockWaveNotifier, send_telegram_message
from core.session import clear_session_valid_marker, save_store_session

OUTAGE_ALERT_THRESHOLD = 10
SESSION_SAVE_EVERY_CYCLES = 10
RECYCLE_TAB_EVERY_CYCLES = 60


def _norm_fold(text: str) -> str:
    """Fold Turkish İ/I/ı and English I/i safely to ASCII 'i' without combining dot artifacts."""
    return (
        (text or "")
        .replace("İ", "i")
        .replace("I", "i")
        .replace("ı", "i")
        .lower()
        .replace("\u0307", "")
    )


async def handle_country_and_cookies(page: Page) -> None:
    """Close Mango cookie banner and any country confirmation modal."""
    country_selectors = [
        "[role='dialog'] button:has-text('Türkiye')",
        "[role='dialog'] button:has-text('Turkey')",
        "[role='dialog'] button:has-text('Stay in Turkey')",
        "[role='dialog'] button:has-text(\"Türkiye'de kal\")",
        "[role='dialog'] button:has-text('Continue to Turkey')",
        "[role='dialog'] button:has-text('Devam et')",
        "[role='dialog'] button:has-text('Continue')",
        "[class*='CountrySelector'] button:has-text('Türkiye')",
    ]
    matched_country = await click_first_visible(
        page, country_selectors, timeout_ms=1000, post_wait_ms=800
    )
    if matched_country:
        print(f"[Mango] Ülke seçimi onaylandı ({matched_country}).")

    cookie_selectors = [
        "button[id='cookies.button.acceptAll']",
        "#onetrust-accept-btn-handler",
        "button:has-text('Tümünü kabul et')",
        "button:has-text('TÜMÜNÜ KABUL ET')",
        "button:has-text('Accept all')",
    ]
    matched_cookie = await click_first_visible(
        page, cookie_selectors, timeout_ms=1000, post_wait_ms=800
    )
    if matched_cookie:
        print(f"[Mango] Çerezler kabul edildi ({matched_cookie}).")


async def has_block_or_captcha(page: Page) -> bool:
    """Detect WAF/Vercel security checkpoints or CAPTCHA iframes without bypassing them."""
    block_markers = tuple(
        _norm_fold(m)
        for m in (
            "access denied",
            "attention required",
            "vercel security checkpoint",
            "vercel güvenlik kontrol noktası",
            "tarayıcınız doğrulanamadı",
            "tarayıcınızı doğruluyoruz",
            "failed to verify your browser",
            "we're verifying your browser",
            "checking your browser",
            "verify you are human",
            "insan olduğunuzu doğrulayın",
            "just a moment",
        )
    )
    try:
        title = _norm_fold(await page.title() or "")
        if any(k in title for k in block_markers):
            return True
    except Exception:
        pass
    try:
        text = _norm_fold(await page.locator("body").inner_text(timeout=2500))
        if any(k in text for k in block_markers):
            return True
    except Exception:
        pass
    captcha_selectors = (
        "iframe[src*='captcha']",
        "iframe[src*='arkose']",
        "iframe[title*='reCAPTCHA']",
        "iframe[src*='turnstile']",
        "#sec-overlay",
    )
    for sel in captcha_selectors:
        try:
            visible_loc = page.locator(f"{sel}:visible")
            if await visible_loc.count() > 0 and await visible_loc.first.is_visible(timeout=250):
                return True
        except Exception:
            continue
    return False


def _summarize_json_payload(raw_text: str) -> str:
    """Summarize JSON structure using only top-level keys and container lengths (no values)."""
    try:
        data = json.loads(raw_text)
    except Exception:
        return "json_parse_err"

    if isinstance(data, dict):
        keys = list(data.keys())[:10]
        sizes = [
            f"{k}:{len(v)}"
            for k, v in data.items()
            if isinstance(v, (list, dict))
        ][:8]
        return f"keys={keys} lens={{{','.join(sizes)}}}"
    if isinstance(data, list):
        first_keys = list(data[0].keys())[:10] if data and isinstance(data[0], dict) else []
        return f"list_len={len(data)} item_keys={first_keys}"
    return f"type={type(data).__name__}"


def _attach_mango_network_logger(page: Page) -> tuple[list[str], Callable[[], None]]:
    """Attach a bounded, secret-free response logger for Mango favorites/auth API calls."""
    logs: list[str] = []
    state = {"active": True}
    static_exts = (
        ".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp",
        ".woff", ".woff2", ".ico", ".map", ".avif", ".gif",
    )
    interesting = (
        "wishlist", "favorite", "favori", "saved", "account",
        "auth", "session", "token", "customer", "/user", "/api/",
    )

    async def _on_response(resp: Response) -> None:
        if not state["active"] or len(logs) >= 35:
            return
        try:
            path = resp.url.split("?")[0]
            lower_path = path.lower()
            if lower_path.endswith(static_exts):
                return
            ct = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            is_interesting = (
                "json" in ct
                or resp.status >= 400
                or any(tok in lower_path for tok in interesting)
            )
            if not is_interesting:
                return

            size_hdr = resp.headers.get("content-length") or "?"
            if "json" in ct and resp.status < 400:
                try:
                    body = await resp.text()
                    preview = _summarize_json_payload(body)
                    logs.append(f"{resp.status} {path} ct={ct} len={len(body)} {preview}")
                    return
                except Exception:
                    pass
            logs.append(f"{resp.status} {path} ct={ct or '-'} len={size_hdr}")
        except Exception:
            pass

    page.on("response", _on_response)

    def _stop() -> None:
        if state["active"]:
            state["active"] = False
            try:
                page.remove_listener("response", _on_response)
            except Exception:
                pass

    return logs, _stop


def _has_mango_api_auth_failure(net_logs: list[str]) -> bool:
    """Return True when Mango favorites/wishlist/auth API endpoints return 401/403."""
    auth_tokens = ("wishlist", "favorite", "favori", "saved", "account", "auth", "session", "/user", "customer")
    for entry in net_logs:
        lower_entry = entry.lower()
        if entry.startswith(("401 ", "403 ")) and any(tok in lower_entry for tok in auth_tokens):
            return True
    return False


async def _mango_dom_diagnostics(page: Page) -> str:
    """Return concise <main> DOM metrics and sample text for troubleshooting 0-product loads."""
    try:
        info = await page.evaluate(
            """
            () => {
                const root = document.querySelector('main, [role="main"]') || document.body;
                if (!root) return { err: "no_root" };
                const counts = {
                    a: root.querySelectorAll('a').length,
                    p_links: root.querySelectorAll('a[href*="/p/"]').length,
                    virtual_items: root.querySelectorAll('.virtual-item').length,
                    cards: root.querySelectorAll('[class*="ProductCard"], [class*="productCard"], [data-testid*="product-card"]').length,
                    buttons: root.querySelectorAll('button').length,
                };
                const text = (root.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 320);
                return { root_tag: root.tagName.toLowerCase(), counts, text };
            }
            """
        )
        return f"root={info.get('root_tag')} counts={info.get('counts')} text={info.get('text')}"
    except Exception as exc:
        return f"dom_diag_err={exc}"


async def _wait_for_mango_favorites_ready(page: Page, timeout_ms: int = 12000) -> str:
    """Wait until Mango favorites SPA hydrates products, shows login/block, or times out."""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    await handle_country_and_cookies(page)

    checked_modal_again = False
    started = time.monotonic()

    while time.monotonic() < deadline:
        url = (page.url or "").lower()
        if "login" in url or "signin" in url:
            return "login"

        if await has_block_or_captcha(page):
            return "blocked"

        try:
            ready = await page.evaluate(
                """
                () => {
                    const hasProducts = document.querySelectorAll(
                        'main a[href*="/p/"], a[href*="/p/"], .virtual-item a[href], [class*="ProductCard"] a[href]'
                    ).length > 0;
                    const hasLoginInput = document.querySelector(
                        "input[id='login-form-email-input'], input[type='email'], input[type='password']"
                    ) !== null;
                    return { hasProducts, hasLoginInput };
                }
                """
            )
            if ready.get("hasProducts"):
                # Allow sibling cards in the same render batch to finish painting.
                await page.wait_for_timeout(500)
                return "products"
            if ready.get("hasLoginInput"):
                return "login"
        except Exception:
            pass

        if not checked_modal_again and (time.monotonic() - started) >= 1.5:
            checked_modal_again = True
            await handle_country_and_cookies(page)

        await page.wait_for_timeout(400)

    return "empty_or_timeout"


async def _inspect_mango_page(page: Page) -> dict:
    """Inspect current Mango page URL, product links, login form, and main login CTA state."""
    has_login_form = False
    try:
        has_login_form = await page.locator(
            "input[id='login-form-email-input'], input[type='email'], input[type='password']"
        ).first.is_visible(timeout=1000)
    except Exception:
        pass

    info = await page.evaluate(
        """
        () => {
            const normFold = (s) => (s || "").replace(/[İIı]/g, "i").toLowerCase().replace(/\\u0307/g, "");
            const url = (window.location.href || "").toLowerCase();
            const mainLinks = document.querySelectorAll('main a[href*="/p/"], a[href*="/p/"]');
            const seen = new Set();
            mainLinks.forEach(a => {
                if (a.href) seen.add(a.href);
            });
            const mainEl = document.querySelector('main, [role="main"]');
            const mainText = mainEl ? normFold(mainEl.innerText) : "";
            const bodyText = document.body ? normFold(document.body.innerText) : "";
            const targetText = mainText || bodyText;
            const hasCtaWords = ["giriş yap", "oturum aç", "sign in", "log in"]
                .map(normFold)
                .some(k => targetText.includes(k));
            const hasEmptyText = [
                "favori ürün listeniz boş",
                "favori listeniz boş",
                "henüz favori",
                "favourites list is empty",
                "favorites list is empty"
            ].map(normFold).some(k => targetText.includes(k));
            return {
                is_tr_store: url.includes("/tr/"),
                is_favorites_url: ["favorites", "favori", "wishlist", "saved"].some(k => url.includes(k)),
                has_login_url: url.includes("login") || url.includes("signin"),
                total_product_links: seen.size,
                has_login_cta_in_main: seen.size === 0 && hasCtaWords,
                has_empty_favorites_text: seen.size === 0 && hasEmptyText
            };
        }
        """
    )
    info["has_login_form"] = has_login_form
    return info


async def _is_mango_session_active(page: Page, state: dict) -> bool:
    """Return True when page is verified on TR Mango favorites without login or block screens."""
    return (
        not state["has_login_url"]
        and not state["has_login_form"]
        and not state["has_login_cta_in_main"]
        and state["is_tr_store"]
        and state["is_favorites_url"]
        and not await has_block_or_captcha(page)
    )


async def _clear_stale_session_state(context: BrowserContext, page: Page) -> None:
    """Clear expired/corrupt cookies and web storage before starting a fresh login."""
    try:
        await context.clear_cookies()
    except Exception as exc:
        print(f"[UYARI] Mango eski çerezler temizlenemedi: {exc}")
    try:
        if not page.is_closed():
            await page.evaluate(
                "() => { try { localStorage.clear(); } catch (e) {} try { sessionStorage.clear(); } catch (e) {} }"
            )
    except Exception:
        pass


async def _ensure_mango_login_with_reason(
    context: BrowserContext, page: Page
) -> tuple[bool, str]:
    """Verify Mango session on favorites or perform login; return (ok, reason_code)."""
    net_logs, stop_net_logger = _attach_mango_network_logger(page)
    try:
        await safe_goto(page, MANGO_FAVORITES_URL, timeout_ms=30000, retries=2)
        ready_state = await _wait_for_mango_favorites_ready(page, timeout_ms=12000)

        if ready_state == "blocked" or await has_block_or_captcha(page):
            print(
                "[HATA] Mango favoriler sayfasında Captcha/WAF/Vercel engeli algılandı -> "
                f"{await page_diagnostics(page)}"
            )
            return False, "blocked"

        if "/tr/" not in (page.url or "").lower():
            await safe_goto(page, MANGO_FAVORITES_URL, timeout_ms=30000, retries=2)
            ready_state = await _wait_for_mango_favorites_ready(page, timeout_ms=12000)
            if ready_state == "blocked" or await has_block_or_captcha(page):
                print(
                    "[HATA] Mango TR yönlendirmesinde Captcha/WAF/Vercel engeli algılandı -> "
                    f"{await page_diagnostics(page)}"
                )
                return False, "blocked"

        diag = await page_diagnostics(page)
        state = await _inspect_mango_page(page)
        print(
            f"[Mango] İlk sayfa kontrolü -> {diag} | ready={ready_state} tr={state['is_tr_store']} "
            f"fav={state['is_favorites_url']} urun_link={state['total_product_links']} "
            f"login_form={state['has_login_form']} login_cta={state['has_login_cta_in_main']} "
            f"empty_text={state['has_empty_favorites_text']}"
        )
        if state["total_product_links"] == 0:
            print(f"[Mango] DOM teşhis -> {await _mango_dom_diagnostics(page)}")
            print(f"[Mango] Ağ teşhis ({len(net_logs)} yanıt) -> {net_logs}")

        api_auth_failed = _has_mango_api_auth_failure(net_logs)
        if api_auth_failed:
            print(
                "[UYARI] Mango favoriler/oturum API yanıtı 401/403 döndü; "
                "kayıtlı oturum geçersiz sayılıyor."
            )

        if not api_auth_failed and await _is_mango_session_active(page, state):
            print("[Mango] ✅ Kayıtlı oturum aktif, doğrudan favorilere girildi.")
            await save_store_session(context, "mango", mark_valid=True)
            return True, "ok"
    except Exception as exc:
        print(f"[HATA] Mango başlangıç oturum kontrolü hatası: {exc}")
        return False, "network_error"
    finally:
        stop_net_logger()

    clear_session_valid_marker("mango")

    if not COMMON_EMAIL or not COMMON_PASSWORD:
        print("[HATA] COMMON_EMAIL veya COMMON_PASSWORD eksik.")
        return False, "missing_credentials"

    print("[Mango] Oturum kapalı, eski çerezler temizlenip otomatik giriş başlatılıyor...")
    await _clear_stale_session_state(context, page)
    login_net_logs, stop_login_net_logger = _attach_mango_network_logger(page)
    try:
        await safe_goto(page, MANGO_LOGIN_URL, timeout_ms=30000, retries=2)
        await page.wait_for_timeout(3000)
        await handle_country_and_cookies(page)

        if await has_block_or_captcha(page):
            print(
                "[HATA] Mango giriş sayfasında Captcha/WAF/Vercel engeli çıktı -> "
                f"{await page_diagnostics(page)}"
            )
            return False, "blocked"

        email_input = page.locator(
            "input[id='login-form-email-input'], input[name='email'], input[type='email']"
        ).first
        if not await email_input.is_visible(timeout=3000):
            login_icon = page.locator(
                "a:has-text('Giriş yap'), button:has-text('Giriş yap'), "
                "a:has-text('Oturum aç'), button:has-text('Oturum aç'), a:has-text('Giriş')"
            ).first
            try:
                if await login_icon.is_visible(timeout=3000):
                    await login_icon.click()
                    await page.wait_for_timeout(2000)
            except Exception:
                pass

        await email_input.wait_for(state="visible", timeout=15000)
        await human_type(email_input, COMMON_EMAIL)
        await page.wait_for_timeout(800)

        pass_input = page.locator(
            "input[id='login-form-password-input'], input[name='password'], input[type='password']"
        ).first
        await pass_input.wait_for(state="visible", timeout=15000)
        await human_type(pass_input, COMMON_PASSWORD)
        await page.wait_for_timeout(800)

        await page.locator(
            "button[id='login-form-submit-button']:visible, "
            "form button:has-text('OTURUM AÇ'):visible, "
            "form button:has-text('Oturum aç'):visible, "
            "form button[type='submit']:visible"
        ).first.click()
        await page.wait_for_timeout(6000)

        if await has_block_or_captcha(page):
            print(
                "[HATA] Mango giriş gönderiminde Captcha/WAF/Vercel engeli çıktı -> "
                f"{await page_diagnostics(page)}"
            )
            return False, "blocked"

        login_net_logs.clear()
        await safe_goto(page, MANGO_FAVORITES_URL, timeout_ms=30000, retries=2)
        ready_state = await _wait_for_mango_favorites_ready(page, timeout_ms=12000)

        if ready_state == "blocked" or await has_block_or_captcha(page):
            print(
                "[HATA] Mango giriş sonrası favoriler sayfasında Captcha/WAF/Vercel engeli -> "
                f"{await page_diagnostics(page)}"
            )
            return False, "blocked"

        if "/tr/" not in (page.url or "").lower():
            await safe_goto(page, MANGO_FAVORITES_URL, timeout_ms=30000, retries=2)
            await _wait_for_mango_favorites_ready(page, timeout_ms=12000)

        post_state = await _inspect_mango_page(page)
        if _has_mango_api_auth_failure(login_net_logs) or not await _is_mango_session_active(page, post_state):
            print(f"[HATA] Mango girişi tamamlanamadı -> {await page_diagnostics(page)}")
            print(f"[Mango] Giriş sonrası ağ teşhis ({len(login_net_logs)} yanıt) -> {login_net_logs}")
            return False, "login_failed"

        print("[Mango] ✅ Otomatik giriş başarılı!")
        await save_store_session(context, "mango", mark_valid=True)
        return True, "ok"
    except Exception as exc:
        print(f"[HATA] Mango login hatası: {exc}")
        return False, "login_failed"
    finally:
        stop_login_net_logger()


async def ensure_mango_login(context: BrowserContext, page: Page) -> bool:
    """Verify Mango session on favorites or perform login."""
    ok, _ = await _ensure_mango_login_with_reason(context, page)
    return ok


async def _extract_mango_candidates(page: Page) -> dict:
    """Extract Mango favorite totals and in-stock candidate items from current DOM."""
    return await page.evaluate(
        """
        () => {
            const normFold = (s) => (s || "").replace(/[İIı]/g, "i").toLowerCase().replace(/\\u0307/g, "");
            const results = [];
            const seenUrls = new Set();

            const extractCard = (card, linkEl) => {
                if (!card || !linkEl) return;
                const href = linkEl.href;
                if (!href || seenUrls.has(href)) return;
                seenUrls.add(href);

                const cardText = normFold(card.innerText);
                if (
                    cardText.includes("tükendi") ||
                    cardText.includes("out of stock") ||
                    cardText.includes("mevcut değil")
                ) {
                    return;
                }

                const nameEl = card.querySelector('[class*="ProductTitle"], [class*="title"], h2, h3');
                const name = nameEl ? nameEl.innerText.trim() : (linkEl.getAttribute("aria-label") || linkEl.innerText.trim() || "Mango Ürün");

                results.push({
                    name: name || "Mango Ürün",
                    url: href,
                    oldPrice: "",
                    newPrice: "",
                    hasDiscount: false
                });
            };

            const cardSelector = '.virtual-item, [class*="ProductCard_productCard"], [class*="ProductCard"], [class*="productCard"], [class*="WishlistItem"], [class*="FavoriteCard"], [data-testid*="product-card"], main article, main li';
            const cards = document.querySelectorAll(cardSelector);

            cards.forEach(card => {
                const linkEl = card.querySelector('a[class*="ProductImage"], a[href*="/p/"], a[href*=".html"]');
                if (!linkEl) return;
                extractCard(card, linkEl);
            });

            if (seenUrls.size === 0) {
                const directLinks = document.querySelectorAll('main a[href*="/p/"], a[href*="/p/"]');
                directLinks.forEach(linkEl => {
                    const card = linkEl.closest('article, li, div') || linkEl.parentElement;
                    extractCard(card, linkEl);
                });
            }

            return { total: seenUrls.size, all_urls: [...seenUrls], items: results };
        }
        """
    )


async def _scroll_and_extract_mango_candidates(
    page: Page, max_steps: int = 12, wait_ms: int = 650
) -> dict:
    """Incrementally scroll and merge Mango favorites so virtualized lists never drop items."""
    all_urls: set[str] = set()
    items_by_url: dict[str, dict] = {}

    def _merge(batch: dict) -> None:
        for u in batch.get("all_urls", []):
            if u:
                all_urls.add(u)
        for item in batch.get("items", []):
            url = item.get("url")
            if url:
                all_urls.add(url)
                items_by_url.setdefault(url, item)

    first_batch = await _extract_mango_candidates(page)
    _merge(first_batch)

    try:
        for _ in range(max_steps):
            metrics = await page.evaluate(
                """
                () => ({
                    y: window.scrollY || 0,
                    vh: window.innerHeight || 1080,
                    sh: document.body ? document.body.scrollHeight : 0
                })
                """
            )
            if metrics["y"] + metrics["vh"] >= metrics["sh"] - 20:
                break

            prev_y = metrics["y"]
            prev_sh = metrics["sh"]
            await page.evaluate("window.scrollBy(0, Math.floor((window.innerHeight || 1080) * 0.85))")
            await page.wait_for_timeout(wait_ms)

            batch = await _extract_mango_candidates(page)
            _merge(batch)

            after = await page.evaluate(
                "() => ({ y: window.scrollY || 0, sh: document.body ? document.body.scrollHeight : 0 })"
            )
            if after["y"] == prev_y and after["sh"] == prev_sh:
                break

        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass

    return {"total": len(all_urls), "items": list(items_by_url.values())}


async def _wait_for_mango_product_ready(tab: Page, timeout_ms: int = 9000) -> str:
    """Wait until Mango product detail price/size section hydrates or a block screen appears."""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if await has_block_or_captcha(tab):
            return "blocked"
        try:
            ready = await tab.evaluate(
                """
                () => {
                    const hasSize = document.querySelector(
                        'li[class*="SizesList_listItem"] button, button[class*="SizeItem_sizeItem"]'
                    ) !== null;
                    const hasPrice = document.querySelector('div[class*="Price_container"]') !== null;
                    const hasTitle = document.querySelector('h1[class*="ProductDetail_title"], h1') !== null;
                    return hasSize || (hasPrice && hasTitle);
                }
                """
            )
            if ready:
                await tab.wait_for_timeout(500)
                return "ready"
        except Exception:
            pass
        await tab.wait_for_timeout(350)
    return "timeout"


async def _open_and_check_tab(
    context: BrowserContext, product: dict, sem: asyncio.Semaphore
) -> tuple[str, dict | None]:
    """Open Mango product in a new tab (bounded by sem); require BOTH discount and TARGET_SIZES stock."""
    target_upper = [str(s).upper() for s in TARGET_SIZES]
    async with sem:
        tab: Page | None = None
        try:
            tab = await context.new_page()
            await safe_goto(tab, product["url"], timeout_ms=30000, retries=2)
            ready = await _wait_for_mango_product_ready(tab, timeout_ms=9000)
            if ready == "blocked":
                print(f"  [UYARI] Mango ürün sekmesinde geçici engel ({product.get('name', 'Ürün')}).")
                return "error", product

            info = await tab.evaluate(
                """
                (targetSizes) => {
                    const normFold = (s) => (s || "").replace(/[İIı]/g, "i").toLowerCase().replace(/\\u0307/g, "");
                    const info = { sizes: [], oldPrice: "", newPrice: "", hasDiscount: false, name: "" };

                    try {
                        const titleEl = document.querySelector('h1[class*="ProductDetail_title"], h1');
                        if (titleEl) info.name = titleEl.innerText.trim();

                        const allSrOnly = document.querySelectorAll('div[class*="Price_container"] span[class*="srOnly"]');
                        allSrOnly.forEach(span => {
                            const txt = span.innerText || "";
                            const low = normFold(txt);
                            const bracketMatch = txt.match(/\\[(.*?)\\]/);
                            const priceMatch = txt.match(/\\d[\\d.,]*\\s*(?:TL|₺)/i);
                            const extracted = bracketMatch ? bracketMatch[1].trim() : (priceMatch ? priceMatch[0].trim() : "");
                            if (low.includes("ilk fiyat") || low.includes("üstü çizili") || low.includes("original price")) {
                                if (extracted) info.oldPrice = extracted;
                            }
                            if (low.includes("güncel fiyat") || low.includes("geçerli fiyat") || low.includes("current price")) {
                                if (extracted) info.newPrice = extracted;
                            }
                        });

                        if (!info.oldPrice || !info.newPrice) {
                            const crossedEl = document.querySelector('div[class*="Price_container"] [class*="crossed"], div[class*="Price_container"] del, div[class*="Price_container"] s');
                            const currentEl = document.querySelector('div[class*="Price_container"] [class*="sale"], div[class*="Price_container"] [class*="current"]');
                            if (crossedEl && !info.oldPrice) info.oldPrice = crossedEl.innerText.trim();
                            if (currentEl && !info.newPrice) info.newPrice = currentEl.innerText.trim();
                        }

                        const discountBadge = document.querySelector('[class*="Price_container"] [class*="discount"], [class*="Price_container"] [class*="sale"]');
                        if ((info.oldPrice && info.newPrice && info.oldPrice !== info.newPrice) || discountBadge) {
                            info.hasDiscount = true;
                        }
                    } catch (e) {}

                    const listItems = document.querySelectorAll('li[class*="SizesList_listItem"] button, button[class*="SizeItem_sizeItem"]');
                    listItems.forEach(btn => {
                        const sizeSpan = btn.querySelector('span[class*="SizeItemContent_sizeInfo"] span:first-child');
                        const text = sizeSpan ? sizeSpan.innerText.trim().toUpperCase() : btn.innerText.trim().toUpperCase();
                        const fullLow = normFold(btn.innerText);

                        const isOutOfStock =
                            (btn.className && btn.className.includes("Unavailable")) ||
                            (btn.id && btn.id.includes("Unavailable")) ||
                            btn.hasAttribute("disabled") ||
                            btn.getAttribute("aria-disabled") === "true" ||
                            btn.querySelector('span[class*="notAvailable"]') !== null ||
                            fullLow.includes("mevcut değil") ||
                            fullLow.includes("tükendi") ||
                            fullLow.includes("haber ver") ||
                            fullLow.includes("not available") ||
                            (btn.querySelector('span[class*="SizeItemContent_meta"]') !== null && fullLow.includes("değil"));

                        if (!isOutOfStock) {
                            const sizeVal = text.split("\\n")[0].trim();
                            for (const t of targetSizes) {
                                if (sizeVal === t || new RegExp("\\\\b" + t + "\\\\b", "i").test(sizeVal)) {
                                    info.sizes.push(t);
                                    break;
                                }
                            }
                        }
                    });

                    info.sizes = [...new Set(info.sizes)];
                    return info;
                }
                """,
                target_upper,
            )

            if info and info.get("hasDiscount") and info.get("sizes"):
                item = dict(product)
                if info.get("name"):
                    item["name"] = info["name"]
                if info.get("oldPrice"):
                    item["oldPrice"] = info["oldPrice"]
                if info.get("newPrice"):
                    item["newPrice"] = info["newPrice"]
                item["hasDiscount"] = True
                item["available_sizes"] = info["sizes"]
                print(f"  ✅ {item['name']} -> Hedef Bedenler: {', '.join(item['available_sizes'])}")
                return "match", item
            return "ok", None
        except Exception as exc:
            print(f"  [HATA] Mango sekme tarama hatası ({product.get('name', 'Ürün')}): {exc}")
            return "error", product
        finally:
            if tab is not None:
                try:
                    if not tab.is_closed():
                        await tab.close()
                except Exception:
                    pass


async def _close_stray_tabs(context: BrowserContext, keep_page: Page) -> None:
    """Close any unexpected extra tabs so 7/24 polling never leaks memory."""
    for p in list(context.pages):
        if p is not keep_page:
            try:
                if not p.is_closed():
                    await p.close()
            except Exception:
                pass


def _format_login_failure_alert(reason: str, mid_run: bool = False) -> str:
    """Build an accurate Telegram alert message for Mango login/session failure."""
    prefix = "🚨 Mango oturumu düştü ve yenilenemedi" if mid_run else "🚨 Mango oturumu açılamadı"
    if reason == "blocked":
        return f"{prefix} (Captcha/WAF/Vercel engeli)."
    if reason == "network_error":
        return f"{prefix} (proxy veya ağ zaman aşımı)."
    if reason == "missing_credentials":
        return f"{prefix} (COMMON_EMAIL/COMMON_PASSWORD eksik)."
    return f"{prefix} (oturum doğrulama/giriş tamamlanamadı)."


async def run_mango_loop(context: BrowserContext, max_run_minutes: int) -> bool:
    """Run Mango favorites polling loop with bounded concurrent discount + size checks."""
    favorites_page = await context.new_page()
    try:
        ok, reason = await _ensure_mango_login_with_reason(context, favorites_page)
        if not ok:
            send_telegram_message(_format_login_failure_alert(reason, mid_run=False))
            return False

        notifier = StockWaveNotifier("MANGO")
        start_time = time.time()
        max_seconds = max_run_minutes * 60 if max_run_minutes > 0 else float("inf")
        cycle = 0
        # Keep proxy + Mango Next.js concurrency bounded to 3 tabs (3 batches for 7 items -> ~30s cadence).
        sem = asyncio.Semaphore(3)
        last_known_total = 0
        consecutive_empty_reads = 0
        consecutive_reload_failures = 0
        consecutive_loop_errors = 0
        outage_alert_sent = False

        while (time.time() - start_time) < max_seconds:
            cycle += 1
            cycle_start = time.time()
            print(f"\n[MANGO — Tur #{cycle}] Favoriler yenileniyor...")

            try:
                needs_navigation = cycle > 1
                if favorites_page.is_closed():
                    print("[UYARI] Mango favoriler sekmesi kapanmış, yeniden açılıyor...")
                    favorites_page = await context.new_page()
                    needs_navigation = True
                elif cycle > 1 and cycle % RECYCLE_TAB_EVERY_CYCLES == 0:
                    print("[Mango] Bellek temizliği için favoriler sekmesi yenileniyor...")
                    try:
                        await favorites_page.close()
                    except Exception:
                        pass
                    favorites_page = await context.new_page()
                    needs_navigation = True

                if needs_navigation:
                    if not await safe_reload_or_goto(
                        favorites_page, MANGO_FAVORITES_URL, timeout_ms=30000
                    ):
                        consecutive_reload_failures += 1
                        if consecutive_reload_failures % 3 == 0:
                            print("[UYARI] Mango sekmesi yenileniyor (ardışık yükleme hatası)...")
                            try:
                                await favorites_page.close()
                            except Exception:
                                pass
                            favorites_page = await context.new_page()
                        if consecutive_reload_failures >= OUTAGE_ALERT_THRESHOLD and not outage_alert_sent:
                            outage_alert_sent = True
                            send_telegram_message(
                                f"⚠️ Mango favoriler sayfasına arka arkaya {consecutive_reload_failures} kez "
                                "erişilemedi (ev proxy/ağ kesintisi olabilir). Tekrar denenmeye devam ediliyor."
                            )
                        remaining = max_seconds - (time.time() - start_time)
                        if remaining <= REFRESH_INTERVAL:
                            break
                        await asyncio.sleep(REFRESH_INTERVAL)
                        continue

                if consecutive_reload_failures > 0:
                    if outage_alert_sent:
                        print("[Mango] ✅ Bağlantı/proxy erişimi tekrar sağlandı.")
                        outage_alert_sent = False
                    consecutive_reload_failures = 0

                wait_ms = 12000 if (cycle == 1 or last_known_total > 0) else 8000
                ready_state = await _wait_for_mango_favorites_ready(favorites_page, timeout_ms=wait_ms)

                if ready_state == "blocked" or await has_block_or_captcha(favorites_page):
                    print(f"[UYARI] Mango geçici engeli algılandı, {ERROR_COOLDOWN} sn bekleniyor...")
                    remaining = max_seconds - (time.time() - start_time)
                    if remaining <= ERROR_COOLDOWN:
                        break
                    await asyncio.sleep(ERROR_COOLDOWN)
                    continue

                current_url = (favorites_page.url or "").lower()
                if "login" in current_url or "signin" in current_url:
                    print("[UYARI] Mango oturumu düştü, yeniden giriş deneniyor...")
                    clear_session_valid_marker("mango")
                    re_ok, re_reason = await _ensure_mango_login_with_reason(context, favorites_page)
                    if not re_ok:
                        send_telegram_message(_format_login_failure_alert(re_reason, mid_run=True))
                        return False
                    current_url = (favorites_page.url or "").lower()

                if "/tr/" not in current_url or not any(
                    k in current_url for k in ("favorites", "favori", "wishlist", "saved")
                ):
                    print("[UYARI] Mango favori sayfası dışına çıkıldığı algılandı, favorilere dönülüyor...")
                    try:
                        await safe_goto(favorites_page, MANGO_FAVORITES_URL, timeout_ms=30000, retries=2)
                        await _wait_for_mango_favorites_ready(favorites_page, timeout_ms=12000)
                    except Exception as exc:
                        print(f"[HATA] Mango favorilere dönülemedi: {exc}")
                        remaining = max_seconds - (time.time() - start_time)
                        if remaining <= REFRESH_INTERVAL:
                            break
                        await asyncio.sleep(REFRESH_INTERVAL)
                        continue
                    current_url = (favorites_page.url or "").lower()
                    if "login" in current_url or "signin" in current_url:
                        print("[UYARI] Mango oturumu düştü, yeniden giriş deneniyor...")
                        clear_session_valid_marker("mango")
                        re_ok, re_reason = await _ensure_mango_login_with_reason(context, favorites_page)
                        if not re_ok:
                            send_telegram_message(_format_login_failure_alert(re_reason, mid_run=True))
                            return False

                extracted = await _scroll_and_extract_mango_candidates(favorites_page)
                total = extracted.get("total", 0)
                candidates = extracted.get("items", [])

                # Retry once on unexpected 0-item read to overcome slow SPA hydration over proxy.
                if total == 0 and last_known_total > 0:
                    print(
                        f"[UYARI] Mango 0 ürün gördü (önceki tur: {last_known_total}); "
                        "SPA yüklenmesi için sayfa bir kez tazeleniyor..."
                    )
                    try:
                        await safe_goto(favorites_page, MANGO_FAVORITES_URL, timeout_ms=30000, retries=2)
                        await _wait_for_mango_favorites_ready(favorites_page, timeout_ms=12000)
                        extracted = await _scroll_and_extract_mango_candidates(favorites_page)
                        total = extracted.get("total", 0)
                        candidates = extracted.get("items", [])
                    except Exception as exc:
                        print(f"[UYARI] Mango boş liste kurtarma denemesi başarısız: {exc}")

                print(f"[Mango] Favorilerde toplam {total} ürün görüldü ({len(candidates)} stokta aday).")
                if cycle == 1 and total == 0:
                    print(f"[Mango] İlk tur 0 ürün teşhisi -> {await page_diagnostics(favorites_page)}")
                    print(f"[Mango] İlk tur DOM teşhis -> {await _mango_dom_diagnostics(favorites_page)}")

                if total == 0:
                    if await has_block_or_captcha(favorites_page):
                        print(f"[UYARI] Mango boş sayfada geçici engel algılandı, {ERROR_COOLDOWN} sn bekleniyor...")
                        remaining = max_seconds - (time.time() - start_time)
                        if remaining <= ERROR_COOLDOWN:
                            break
                        await asyncio.sleep(ERROR_COOLDOWN)
                        continue

                    loop_state = await _inspect_mango_page(favorites_page)
                    if (
                        loop_state["has_login_url"]
                        or loop_state["has_login_form"]
                        or loop_state["has_login_cta_in_main"]
                    ):
                        print(
                            "[UYARI] Mango oturumu sayfa içinde düştü "
                            "(login_cta/form algılandı), yeniden giriş deneniyor..."
                        )
                        clear_session_valid_marker("mango")
                        re_ok, re_reason = await _ensure_mango_login_with_reason(context, favorites_page)
                        if not re_ok:
                            send_telegram_message(_format_login_failure_alert(re_reason, mid_run=True))
                            return False
                        continue

                    consecutive_empty_reads += 1
                    if last_known_total > 0 and consecutive_empty_reads < 2:
                        print(
                            "[UYARI] Mango geçici 0 ürün okudu; yanlış 'Stok Tükendi' bildirimini "
                            "önlemek için bu turda bildirim ve oturum kaydı atlandı -> "
                            f"{await page_diagnostics(favorites_page)}"
                        )
                        remaining = max_seconds - (time.time() - start_time)
                        if remaining <= REFRESH_INTERVAL:
                            break
                        await asyncio.sleep(REFRESH_INTERVAL)
                        continue
                else:
                    last_known_total = total
                    consecutive_empty_reads = 0

                matched_items: list[dict] = []
                errored_urls: set[str] = set()
                errored_tabs = 0
                if candidates:
                    results = await asyncio.gather(
                        *[_open_and_check_tab(context, prod, sem) for prod in candidates],
                        return_exceptions=True,
                    )
                    for prod, res in zip(candidates, results):
                        if isinstance(res, Exception):
                            errored_tabs += 1
                            if prod.get("url"):
                                errored_urls.add(prod["url"])
                            print(f"  [HATA] Mango sekme istisnası: {res}")
                        else:
                            status, item = res
                            if status == "match" and item is not None:
                                matched_items.append(item)
                            elif status == "error":
                                errored_tabs += 1
                                if prod.get("url"):
                                    errored_urls.add(prod["url"])

                if candidates and errored_tabs == len(candidates):
                    print(
                        "[UYARI] Mango tüm ürün sekmeleri hata verdi; yanlış 'Stok Tükendi' "
                        "önlemek için bildirim değerlendirmesi atlandı."
                    )
                else:
                    notifier.process_cycle(matched_items, uncertain_urls=errored_urls)

                if total > 0 and (cycle == 1 or cycle % SESSION_SAVE_EVERY_CYCLES == 0):
                    await save_store_session(context, "mango", mark_valid=True)

                consecutive_loop_errors = 0
            except Exception as exc:
                consecutive_loop_errors += 1
                print(f"[HATA] Mango tur #{cycle} beklenmeyen hata ({consecutive_loop_errors}): {exc}")
                if consecutive_loop_errors >= 3:
                    try:
                        if not favorites_page.is_closed():
                            await favorites_page.close()
                    except Exception:
                        pass
                    try:
                        favorites_page = await context.new_page()
                    except Exception:
                        pass
            finally:
                await _close_stray_tabs(context, favorites_page)

            cycle_elapsed = time.time() - cycle_start
            min_sleep = min(10.0, float(REFRESH_INTERVAL))
            sleep_for = max(min_sleep, float(REFRESH_INTERVAL) - cycle_elapsed)
            remaining = max_seconds - (time.time() - start_time)
            if remaining <= sleep_for:
                break

            print(
                f"[Mango] Tarama tamamlandı ({cycle_elapsed:.1f} sn). "
                f"{sleep_for:.1f} sn bekleniyor..."
            )
            await asyncio.sleep(sleep_for)

        if consecutive_reload_failures >= OUTAGE_ALERT_THRESHOLD:
            print(
                f"[HATA] Mango koşusu kesintisiz bağlantı/proxy arızası "
                f"({consecutive_reload_failures} ardışık hata) ile sona erdi."
            )
            return False

        return True
    finally:
        try:
            if not favorites_page.is_closed():
                await favorites_page.close()
        except Exception:
            pass
