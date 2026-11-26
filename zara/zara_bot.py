import asyncio
import json
import time
from patchright.async_api import BrowserContext, Page, Response
from config import (
    COMMON_EMAIL,
    COMMON_PASSWORD,
    ERROR_COOLDOWN,
    REFRESH_INTERVAL,
    TARGET_SIZES,
    ZARA_FAVORITES_URL,
    ZARA_HOME_URL,
    ZARA_LOGIN_URL,
)
from core.browser import (
    click_first_visible,
    human_type,
    page_diagnostics,
    safe_goto,
    safe_reload_or_goto,
    scroll_page_to_bottom,
)
from core.notifier import StockWaveNotifier, send_telegram_message
from core.session import clear_session_valid_marker, save_store_session

PAGE_RECYCLE_INTERVAL_CYCLES = 60
SESSION_SAVE_INTERVAL_SECONDS = 900
MAX_CONSECUTIVE_ERRORS = 12


def _normalize_tr_lower(text: str) -> str:
    """Lowercase text safely for both Turkish ('İ' -> 'i') and ASCII ('I' -> 'i')."""
    return text.replace("İ", "i").lower().replace("\u0307", "")


async def handle_country_and_cookies(page: Page) -> None:
    """Close Zara country selection modal, cookie banner, and error dialogs."""
    country_selectors = [
        "button[data-qa-action='stay-in-store']",
        "[role='dialog'] button[data-qa-action='stay-in-store']",
        "[role='dialog'] button:has-text('Türkiye')",
        "[role='dialog'] button:has-text('TÜRKİYE')",
        "[role='dialog'] button:has-text('Continue to Turkey')",
        "[role='dialog'] button:has-text('Türkiye mağazasında kal')",
    ]
    matched_country = await click_first_visible(
        page, country_selectors, timeout_ms=2000, post_wait_ms=1500
    )
    if matched_country:
        print(f"[Zara] Türkiye mağaza seçimi onaylandı ({matched_country}).")

    cookie_selectors = [
        "#onetrust-accept-btn-handler",
        "button[data-qa-action='accept-all']",
        "button:has-text('TÜM ÇEREZLERİ KABUL ET')",
        "button:has-text('Tüm çerezleri kabul et')",
        "button:has-text('ACCEPT ALL')",
    ]
    matched_cookie = await click_first_visible(
        page, cookie_selectors, timeout_ms=2000, post_wait_ms=1000
    )
    if matched_cookie:
        print(f"[Zara] Çerezler kabul edildi ({matched_cookie}).")

    error_dialog_selectors = [
        "[role='dialog'] button:has-text('OK')",
        "[role='dialog'] button:has-text('Ok')",
        "[role='dialog'] button:has-text('Tamam')",
        "[role='dialog'] button:has-text('TAMAM')",
    ]
    matched_dialog = await click_first_visible(
        page, error_dialog_selectors, timeout_ms=1000, post_wait_ms=1000
    )
    if matched_dialog:
        print(f"[Zara] Hata diyaloğu kapatıldı ({matched_dialog}).")


async def has_block_or_captcha(page: Page) -> bool:
    """Detect WAF blocks or CAPTCHA overlays without attempting to bypass them."""
    block_markers = (
        "access denied",
        "bir sorun oluştu",
        "something went wrong",
        "verify you are human",
        "insan olduğunuzu doğrulayın",
    )
    try:
        title = _normalize_tr_lower(await page.title() or "")
        if any(marker in title for marker in ("access denied", "attention required")):
            return True
    except Exception:
        pass
    try:
        text = _normalize_tr_lower(await page.locator("body").inner_text(timeout=2500))
        if any(marker in text for marker in block_markers):
            return True
        captcha_count = await page.locator(
            "iframe[src*='captcha'], iframe[src*='arkose'], iframe[title*='reCAPTCHA'], #sec-overlay"
        ).count()
        if captcha_count > 0:
            return True
    except Exception:
        pass
    return False


async def _inspect_zara_page(page: Page) -> dict:
    """Inspect current Zara page URL, product links, login form, and main login CTA state."""
    has_login_form = False
    try:
        has_login_form = await page.locator(
            "input[name='email'], input[name='username'], input[name='logonId'], input[name='password'], "
            "input[type='email'], input[type='password']"
        ).first.is_visible(timeout=500)
    except Exception:
        pass

    info = await page.evaluate(
        """
        () => {
            const normLower = (s) => (s || "").replace(/İ/g, "i").toLowerCase().replace(/\\u0307/g, "");
            const url = normLower(window.location.href || "");
            const links = document.querySelectorAll('a[href*="-p"]');
            const seen = new Set();
            links.forEach(a => {
                if (a.href && /-p\\d{4,}\\.html($|\\?)/.test(a.href)) {
                    seen.add(a.href.split("?")[0]);
                }
            });
            const mainEl = document.querySelector('main, [role="main"]') || document.body;
            const mainText = mainEl ? normLower(mainEl.innerText || "") : "";
            const hasNavToLogin = !!(
                mainEl && mainEl.querySelector(
                    '[data-qa-action="nav-to-login"], [data-qa-id="nav-to-login"], [data-qa-qualifier="nav-to-login"]'
                )
            );
            const hasCtaWords = [
                "giriş yap",
                "oturum aç",
                "oturum açmalısınız",
                "log in",
                "sign in"
            ].some(k => mainText.includes(k));
            const hasEmptyText = [
                "henüz favori ürününüz yok",
                "favori listeniz boş",
                "listeniz boş",
                "your list is empty",
                "no items saved"
            ].some(k => mainText.includes(k));
            return {
                is_tr_store: url.includes("/tr/tr"),
                is_wishlist_url: ["wishlist", "lists", "saved"].some(k => url.includes(k)),
                has_login_url: url.includes("logon") || url.includes("login") || url.includes("account.zara.com"),
                total_product_links: seen.size,
                has_login_cta_in_main: seen.size === 0 && (hasNavToLogin || hasCtaWords),
                has_empty_wishlist_text: seen.size === 0 && hasEmptyText,
            };
        }
        """
    )
    info["has_login_form"] = has_login_form
    return info


async def _is_zara_session_active(page: Page, state: dict) -> bool:
    """Return True when page is verified on TR Zara wishlist without login or block screens."""
    return (
        not state["has_login_url"]
        and not state["has_login_form"]
        and not state["has_login_cta_in_main"]
        and state["is_tr_store"]
        and state["is_wishlist_url"]
        and not await has_block_or_captcha(page)
    )


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


class _ZaraNetworkMonitor:
    """Lightweight per-page Zara API monitor that never misses OAuth/wishlist auth failures."""

    STATIC_EXTS = (
        ".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp",
        ".woff", ".woff2", ".ico", ".map", ".avif", ".gif",
    )
    INTERESTING = ("wishlist", "itxrest", "/lists", "saved", "favorite", "/user/", "/api/")

    def __init__(self, page: Page, capture_details: bool = True) -> None:
        self._page = page
        self._active = True
        self.capture_details = capture_details
        self.logs: list[str] = []
        self.auth_failures: list[str] = []
        self.wishlist_statuses: list[int] = []
        self.api_server_errors: list[str] = []
        self._page.on("response", self._on_response)

    def reset_cycle(self, capture_details: bool | None = None) -> None:
        """Clear per-cycle counters while keeping the listener attached."""
        if capture_details is not None:
            self.capture_details = capture_details
        self.logs.clear()
        self.auth_failures.clear()
        self.wishlist_statuses.clear()
        self.api_server_errors.clear()

    def has_auth_failure(self) -> bool:
        """Return True when wishlist or OAuth endpoints reported an auth rejection."""
        return bool(self.auth_failures)

    def has_wishlist_server_error(self) -> bool:
        """Return True when Zara wishlist API returned 5xx in this cycle."""
        return bool(self.api_server_errors)

    def wishlist_api_succeeded(self) -> bool:
        """Return True when at least one /user/wishlist(s) call returned 2xx."""
        return any(200 <= s < 300 for s in self.wishlist_statuses)

    def stop(self) -> None:
        """Detach response listener safely."""
        if self._active:
            self._active = False
            try:
                self._page.remove_listener("response", self._on_response)
            except Exception:
                pass

    async def _on_response(self, resp: Response) -> None:
        if not self._active:
            return
        try:
            path = resp.url.split("?")[0]
            lower_path = path.lower()
            status = resp.status

            # Always record critical auth/wishlist signals synchronously before any body read.
            if "/user/wishlist" in lower_path:
                if len(self.wishlist_statuses) < 20:
                    self.wishlist_statuses.append(status)
                if status in (401, 403) and len(self.auth_failures) < 20:
                    self.auth_failures.append(f"{status} {path}")
                elif status >= 500 and len(self.api_server_errors) < 20:
                    self.api_server_errors.append(f"{status} {path}")
            elif "/user/oauth2/refresh" in lower_path and status in (400, 401, 403):
                if len(self.auth_failures) < 20:
                    self.auth_failures.append(f"{status} {path}")
            elif "account.zara.com/api/oauth2/authorize" in lower_path and status in (401, 403):
                if len(self.auth_failures) < 20:
                    self.auth_failures.append(f"{status} {path}")

            if not self.capture_details or len(self.logs) >= 35:
                return
            if lower_path.endswith(self.STATIC_EXTS):
                return

            ct = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            is_interesting = (
                "json" in ct
                or status >= 400
                or any(tok in lower_path for tok in self.INTERESTING)
            )
            if not is_interesting:
                return

            size_hdr = resp.headers.get("content-length") or "?"
            if "json" in ct and status < 400:
                try:
                    body = await resp.text()
                    preview = _summarize_json_payload(body)
                    self.logs.append(f"{status} {path} ct={ct} len={len(body)} {preview}")
                    return
                except Exception:
                    pass
            self.logs.append(f"{status} {path} ct={ct or '-'} len={size_hdr}")
        except Exception:
            pass


async def _zara_dom_diagnostics(page: Page) -> str:
    """Return concise <main> DOM metrics, data-qa attributes, links, and shadow root status."""
    try:
        info = await page.evaluate(
            """
            () => {
                const root = document.querySelector('main, [role="main"]') || document.body;
                if (!root) return { err: "no_root" };
                const counts = {
                    a: root.querySelectorAll('a').length,
                    li: root.querySelectorAll('li').length,
                    article: root.querySelectorAll('article').length,
                    button: root.querySelectorAll('button').length,
                    img: root.querySelectorAll('img').length,
                    iframe: root.querySelectorAll('iframe').length,
                    productid: root.querySelectorAll('[data-productid]').length,
                };
                const qaSet = new Set();
                root.querySelectorAll('[data-qa-qualifier], [data-qa-id], [data-qa-action]').forEach(el => {
                    if (qaSet.size >= 15) return;
                    const v = el.getAttribute('data-qa-qualifier')
                           || el.getAttribute('data-qa-id')
                           || el.getAttribute('data-qa-action');
                    if (v) qaSet.add(v);
                });
                const hrefs = [];
                const seenH = new Set();
                root.querySelectorAll('a[href]').forEach(a => {
                    if (hrefs.length >= 8) return;
                    const h = (a.getAttribute('href') || '').split('?')[0];
                    if (h && !seenH.has(h)) {
                        seenH.add(h);
                        hrefs.push(h);
                    }
                });
                let shadowHosts = 0;
                root.querySelectorAll('*').forEach(el => {
                    if (el.shadowRoot) shadowHosts += 1;
                });
                const text = (root.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 400);
                return {
                    root_tag: root.tagName.toLowerCase(),
                    counts,
                    qa: [...qaSet],
                    hrefs,
                    shadow_hosts: shadowHosts,
                    text,
                };
            }
            """
        )
        return (
            f"root={info.get('root_tag')} counts={info.get('counts')} "
            f"shadow_hosts={info.get('shadow_hosts')} qa={info.get('qa')} "
            f"hrefs={info.get('hrefs')} text={info.get('text')}"
        )
    except Exception as exc:
        return f"dom_diag_err={exc}"


async def _wait_for_zara_wishlist_ready(
    page: Page,
    monitor: _ZaraNetworkMonitor | None = None,
    timeout_ms: int = 10000,
) -> dict:
    """Wait until Zara wishlist SPA hydrates (products, login CTA, empty text, or API rejection)."""
    try:
        await page.wait_for_load_state("networkidle", timeout=min(5000, timeout_ms))
    except Exception:
        pass

    deadline = time.monotonic() + (timeout_ms / 1000.0)
    last_state: dict | None = None
    while True:
        last_state = await _inspect_zara_page(page)
        if (
            last_state["total_product_links"] > 0
            or last_state["has_login_url"]
            or last_state["has_login_form"]
            or last_state["has_login_cta_in_main"]
            or last_state["has_empty_wishlist_text"]
            or (monitor is not None and monitor.has_auth_failure())
        ):
            return last_state
        if await has_block_or_captcha(page):
            return last_state
        if time.monotonic() >= deadline:
            return last_state
        await page.wait_for_timeout(500)


async def _wait_for_login_redirect(page: Page, timeout_ms: int = 12000) -> bool:
    """Wait for Zara OAuth redirect to complete after submitting credentials."""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        url = (page.url or "").lower()
        if "www.zara.com/tr/tr" in url and "logon" not in url and "login" not in url:
            await page.wait_for_timeout(1500)
            return True
        if await has_block_or_captcha(page):
            return False
        await page.wait_for_timeout(500)
    return False


async def ensure_zara_login(context: BrowserContext, page: Page) -> bool:
    """Verify Zara session on wishlist or perform login via home -> logon."""
    monitor = _ZaraNetworkMonitor(page, capture_details=True)
    try:
        nav_ok = False
        for attempt in range(1, 3):
            try:
                await safe_goto(page, ZARA_FAVORITES_URL, timeout_ms=35000, retries=2)
                nav_ok = True
                break
            except Exception as nav_exc:
                if attempt < 2:
                    print(
                        f"[UYARI] Zara başlangıç sayfası açılamadı ({nav_exc}); "
                        "4 sn sonra yeniden deneniyor..."
                    )
                    await asyncio.sleep(4)
                else:
                    raise

        if not nav_ok:
            return False

        await page.wait_for_timeout(2000)
        await handle_country_and_cookies(page)

        if "/tr/tr" not in (page.url or "").lower():
            monitor.reset_cycle(capture_details=True)
            await safe_goto(page, ZARA_FAVORITES_URL, timeout_ms=35000, retries=2)
            await page.wait_for_timeout(2000)
            await handle_country_and_cookies(page)

        state = await _wait_for_zara_wishlist_ready(page, monitor=monitor, timeout_ms=10000)
        diag = await page_diagnostics(page)
        print(
            f"[Zara] İlk sayfa kontrolü -> {diag} | tr={state['is_tr_store']} "
            f"wishlist={state['is_wishlist_url']} urun_link={state['total_product_links']} "
            f"login_form={state['has_login_form']} login_cta={state['has_login_cta_in_main']}"
        )
        if state["total_product_links"] == 0:
            print(f"[Zara] DOM teşhis -> {await _zara_dom_diagnostics(page)}")
            print(f"[Zara] Ağ teşhis ({len(monitor.logs)} yanıt) -> {monitor.logs}")

        api_auth_failed = monitor.has_auth_failure()
        if api_auth_failed:
            print(
                "[UYARI] Zara wishlist/OAuth API reddedildi "
                f"({monitor.auth_failures}); kayıtlı oturum geçersiz sayılıyor."
            )
            clear_session_valid_marker("zara")

        if not api_auth_failed and await _is_zara_session_active(page, state):
            print("[Zara] ✅ Kayıtlı oturum aktif, doğrudan favorilere girildi.")
            await save_store_session(context, "zara", mark_valid=True)
            return True

        clear_session_valid_marker("zara")
    except Exception as exc:
        print(f"[HATA] Zara başlangıç oturum kontrolü hatası: {exc}")
        return False
    finally:
        monitor.stop()

    if not COMMON_EMAIL or not COMMON_PASSWORD:
        print("[HATA] COMMON_EMAIL veya COMMON_PASSWORD eksik.")
        return False

    print("[Zara] Oturum kapalı, otomatik giriş başlatılıyor...")
    post_monitor = _ZaraNetworkMonitor(page, capture_details=True)
    try:
        await safe_goto(page, ZARA_HOME_URL, timeout_ms=35000, retries=2)
        await page.wait_for_timeout(2500)
        await handle_country_and_cookies(page)

        login_clicked = await click_first_visible(
            page,
            [
                "[data-qa-id='layout-desktop-layout-logon-action']",
                "a[href*='logon']",
                "a:has-text('GİRİŞ YAP')",
                "a:has-text('OTURUM AÇ')",
            ],
            timeout_ms=3000,
            post_wait_ms=2500,
        )
        if not login_clicked:
            await safe_goto(page, ZARA_LOGIN_URL, timeout_ms=35000, retries=2)
            await page.wait_for_timeout(3000)

        await handle_country_and_cookies(page)

        if await has_block_or_captcha(page):
            print(
                "[HATA] Zara giriş sayfasında Captcha/WAF/Inditex HLP engeli çıktı "
                f"-> {await page_diagnostics(page)}"
            )
            return False

        email_input = page.locator(
            "input[name='username']:visible, input[type='email']:visible, "
            "input[name='email']:visible, input[name='logonId']:visible, "
            "input[aria-label*='E-posta']:visible"
        ).first
        await email_input.wait_for(state="visible", timeout=15000)
        await human_type(email_input, COMMON_EMAIL)
        await page.wait_for_timeout(800)

        submitted_email = await click_first_visible(
            page,
            [
                "form button:has-text('DEVAM ET')",
                "form button:has-text('Devam et')",
                "form button:has-text('CONTINUE')",
                "form button[data-qa-action='submit']",
                "button.submit-button",
                "main form button[type='submit']",
                "button:has-text('DEVAM ET')",
                "button:has-text('CONTINUE')",
            ],
            timeout_ms=4000,
            post_wait_ms=4000,
        )
        if not submitted_email:
            print(f"[HATA] Zara e-posta devam butonu bulunamadı -> {await page_diagnostics(page)}")
            return False

        await handle_country_and_cookies(page)

        if await has_block_or_captcha(page):
            print(
                "[HATA] E-posta adımında Captcha/WAF veya 'Bir sorun oluştu' tetiklendi "
                f"-> {await page_diagnostics(page)}"
            )
            return False

        pwd_link = page.get_by_text("Şifre ile giriş yap").or_(
            page.get_by_text("Log in with password")
        ).or_(page.get_by_text("Parola ile giriş yap")).first
        try:
            if await pwd_link.is_visible(timeout=2500):
                await pwd_link.click()
                await page.wait_for_timeout(1500)
        except Exception:
            pass

        pass_input = page.locator(
            "input[name='password']:visible, input[type='password']:visible, "
            "input[aria-label*='Parola']:visible, input[aria-label*='Password']:visible"
        ).first
        await pass_input.wait_for(state="visible", timeout=15000)
        await human_type(pass_input, COMMON_PASSWORD)
        await page.wait_for_timeout(800)

        submitted_login = await click_first_visible(
            page,
            [
                "form button:has-text('OTURUM AÇ')",
                "form button:has-text('Oturum aç')",
                "form button:has-text('GİRİŞ YAP')",
                "form button:has-text('LOG IN')",
                "form button:has-text('DEVAM ET')",
                "form button[data-qa-action='submit']",
                "button.submit-button",
                "main form button[type='submit']",
            ],
            timeout_ms=4000,
            post_wait_ms=2000,
        )
        if not submitted_login:
            print(f"[HATA] Zara şifre giriş butonu bulunamadı -> {await page_diagnostics(page)}")
            return False

        await _wait_for_login_redirect(page, timeout_ms=12000)

        if await has_block_or_captcha(page):
            print(
                "[HATA] Şifre adımında Captcha/WAF veya 'Bir sorun oluştu' tetiklendi "
                f"-> {await page_diagnostics(page)}"
            )
            return False

        post_monitor.reset_cycle(capture_details=True)
        await safe_goto(page, ZARA_FAVORITES_URL, timeout_ms=35000, retries=2)
        await page.wait_for_timeout(2000)
        await handle_country_and_cookies(page)

        if "/tr/tr" not in (page.url or "").lower():
            post_monitor.reset_cycle(capture_details=True)
            await safe_goto(page, ZARA_FAVORITES_URL, timeout_ms=35000, retries=2)
            await page.wait_for_timeout(2000)
            await handle_country_and_cookies(page)

        post_state = await _wait_for_zara_wishlist_ready(
            page, monitor=post_monitor, timeout_ms=10000
        )
        if post_monitor.has_auth_failure() or not await _is_zara_session_active(page, post_state):
            print(
                f"[HATA] Zara girişi tamamlanamadı -> {await page_diagnostics(page)} | "
                f"auth_err={post_monitor.auth_failures}"
            )
            return False

        print("[Zara] ✅ Otomatik giriş başarılı!")
        await save_store_session(context, "zara", mark_valid=True)
        return True
    except Exception as exc:
        print(f"[HATA] Zara login hatası: {exc}")
        return False
    finally:
        post_monitor.stop()


async def _extract_stock_and_discounted_items(page: Page) -> dict:
    """Return Zara wishlist totals and items that appear both in stock and discounted."""
    return await page.evaluate(
        """
        () => {
            const normLower = (s) => (s || "").replace(/İ/g, "i").toLowerCase().replace(/\\u0307/g, "");
            const results = [];
            const linksByUrl = new Map();
            const links = document.querySelectorAll('a[href*="-p"]');

            links.forEach(link => {
                const href = link.href || "";
                if (!/-p\\d{4,}\\.html($|\\?)/.test(href)) return;
                const canonical = href.split("?")[0];
                if (!linksByUrl.has(canonical)) {
                    linksByUrl.set(canonical, []);
                }
                linksByUrl.get(canonical).push(link);
            });

            let inStockCount = 0;

            linksByUrl.forEach((urlLinks, canonicalUrl) => {
                let card = null;
                let primaryHref = canonicalUrl;
                let name = "";

                for (const link of urlLinks) {
                    if (!primaryHref) primaryHref = link.href;
                    const candidateCard = link.closest(
                        'li, article, .product-grid-product, .product-grid-product-info, .product-detail-info'
                    ) || link.parentElement?.parentElement?.parentElement;
                    if (!card && candidateCard) card = candidateCard;

                    if (!name && candidateCard) {
                        const nameEl = candidateCard.querySelector(
                            '[data-qa-qualifier="product-detail-secondary-product-info-name"], ' +
                            '[data-qa-qualifier*="product-info-name"], ' +
                            '.product-grid-product-info__main-info a, h2, h3'
                        );
                        if (nameEl && nameEl.innerText.trim()) {
                            name = nameEl.innerText.trim();
                        }
                    }
                    if (!name && link.innerText && link.innerText.trim()) {
                        name = link.innerText.trim();
                    }
                    if (!name) {
                        const aria = link.getAttribute("aria-label") || link.getAttribute("title") || "";
                        if (aria.trim()) name = aria.trim();
                    }
                    if (!name) {
                        const img = link.querySelector("img[alt]");
                        if (img && (img.getAttribute("alt") || "").trim()) {
                            name = img.getAttribute("alt").trim();
                        }
                    }
                }

                if (!card) return;
                if (!name) {
                    const slugMatch = canonicalUrl.match(/\\/([^/]+)-p\\d+\\.html$/);
                    name = slugMatch ? slugMatch[1].replace(/-/g, " ").trim() : "Zara Ürün";
                }

                const cardText = normLower(card.innerText || "");
                const hasStock = !(
                    cardText.includes("tükendi") ||
                    cardText.includes("out of stock") ||
                    cardText.includes("benzer ürünler") ||
                    cardText.includes("coming soon")
                );
                if (!hasStock) return;
                inStockCount += 1;

                let oldPrice = "";
                let newPrice = "";
                const priceEls = card.querySelectorAll('.money-amount__main');
                if (priceEls.length > 0) {
                    const oldEl = card.querySelector('.price-old__amount .money-amount__main');
                    const newEl = card.querySelector('.price-current__amount .money-amount__main');
                    if (oldEl && newEl) {
                        oldPrice = oldEl.innerText.trim();
                        newPrice = newEl.innerText.trim();
                    } else if (priceEls.length >= 2) {
                        oldPrice = priceEls[0].innerText.trim();
                        newPrice = priceEls[1].innerText.trim();
                    } else {
                        newPrice = priceEls[0].innerText.trim();
                    }
                }

                const discountEl = card.querySelector('[class*="discount-percentage"], [class*="discount"]');
                const hasDiscount = (oldPrice !== "" && newPrice !== "" && oldPrice !== newPrice) || !!discountEl;

                if (hasStock && hasDiscount) {
                    results.push({ name, url: primaryHref, oldPrice, newPrice, hasDiscount: true });
                }
            });

            return { total: linksByUrl.size, in_stock: inStockCount, items: results };
        }
        """
    )


async def _open_and_check_tab(
    context: BrowserContext, product: dict, sem: asyncio.Semaphore
) -> tuple[str, dict | None]:
    """Open product page in a new tab (bounded by sem) and check TARGET_SIZES availability."""
    target_upper = [str(s).upper() for s in TARGET_SIZES]
    async with sem:
        tab: Page | None = None
        try:
            tab = await context.new_page()
            await safe_goto(tab, product["url"], timeout_ms=35000, retries=2)
            try:
                await tab.locator(
                    "button.size-selector-sizes-size__button, li.size-selector-list__item, "
                    "button[data-qa-action*='size'], button:has-text('EKLE'), button:has-text('ADD')"
                ).first.wait_for(state="visible", timeout=6000)
            except Exception:
                await tab.wait_for_timeout(2000)

            if await has_block_or_captcha(tab):
                print(
                    f"  [UYARI] Ürün sekmesinde engel/CAPTCHA algılandı ({product.get('name', 'Ürün')})."
                )
                return "error", product

            try:
                add_btn = tab.locator("button:has-text('EKLE'), button:has-text('ADD')").first
                if await add_btn.is_visible(timeout=1200):
                    await add_btn.click()
                    await tab.wait_for_timeout(1000)
            except Exception:
                pass

            sizes = await tab.evaluate(
                """
                (targetSizes) => {
                    const normLower = (s) => (s || "").replace(/İ/g, "i").toLowerCase().replace(/\\u0307/g, "");
                    const escapeRegExp = (s) => s.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&");
                    const found = [];
                    const buttons = document.querySelectorAll(
                        'button.size-selector-sizes-size__button, li.size-selector-list__item, button[data-qa-action*="size"]'
                    );
                    buttons.forEach(btn => {
                        const rawTxt = (btn.innerText || "").trim();
                        const lowerTxt = normLower(rawTxt);
                        const qaAction = (btn.getAttribute("data-qa-action") || "").toLowerCase();
                        const cls = typeof btn.className === "string" ? btn.className.toLowerCase() : "";
                        const isOut = lowerTxt.includes("tükendi") || lowerTxt.includes("benzer") ||
                                      lowerTxt.includes("coming soon") || btn.hasAttribute("disabled") ||
                                      btn.getAttribute("aria-disabled") === "true" ||
                                      qaAction === "size-out-of-stock" ||
                                      cls.includes("out-of-stock");
                        if (!isOut) {
                            let sizeName = rawTxt.split("\\n")[0].trim().toUpperCase();
                            const lbl = btn.querySelector(".size-selector-sizes-size__label-content");
                            if (lbl && lbl.innerText.trim()) {
                                sizeName = lbl.innerText.trim().toUpperCase();
                            }

                            for (const t of targetSizes) {
                                if (sizeName === t || new RegExp("\\\\b" + escapeRegExp(t) + "\\\\b", "i").test(sizeName)) {
                                    found.push(t);
                                    break;
                                }
                            }
                        }
                    });
                    return [...new Set(found)];
                }
                """,
                target_upper,
            ) or []

            if sizes:
                item = dict(product)
                item["available_sizes"] = sizes
                print(f"  ✅ {item['name']} -> Hedef Bedenler: {', '.join(sizes)}")
                return "match", item
            return "ok", None
        except Exception as exc:
            print(f"  [HATA] Sekme tarama hatası ({product.get('name', 'Ürün')}): {exc}")
            return "error", product
        finally:
            if tab is not None:
                try:
                    if not tab.is_closed():
                        await tab.close()
                except Exception:
                    pass


async def _recycle_favorites_page(
    context: BrowserContext,
    old_page: Page | None,
    old_monitor: _ZaraNetworkMonitor | None,
    capture_details: bool = False,
) -> tuple[Page, _ZaraNetworkMonitor]:
    """Close old wishlist tab and open a fresh one to keep Chromium memory stable over 6h runs."""
    if old_monitor is not None:
        old_monitor.stop()
    if old_page is not None:
        try:
            if not old_page.is_closed():
                await old_page.close()
        except Exception:
            pass
    new_page = await context.new_page()
    new_monitor = _ZaraNetworkMonitor(new_page, capture_details=capture_details)
    return new_page, new_monitor


async def run_zara_loop(context: BrowserContext, max_run_minutes: int) -> bool:
    """Run Zara wishlist polling loop with bounded concurrent size verification."""
    favorites_page = await context.new_page()
    if not await ensure_zara_login(context, favorites_page):
        try:
            if not favorites_page.is_closed():
                await favorites_page.close()
        except Exception:
            pass
        send_telegram_message(
            "🚨 Zara oturumu açılamadı (OAuth süresi dolmuş veya WAF/Captcha engeli). "
            "Yerelde 'python export_session.py --store zara' (giris_zara.bat) ile ZARA_STORAGE_STATE secret'ını yenileyin."
        )
        return False

    notifier = StockWaveNotifier("ZARA")
    start_time = time.time()
    last_session_save = start_time
    max_seconds = max_run_minutes * 60 if max_run_minutes > 0 else float("inf")
    cycle = 0
    consecutive_errors = 0
    consecutive_empty_cycles = 0
    had_products_before = False
    sem = asyncio.Semaphore(3)
    loop_monitor = _ZaraNetworkMonitor(favorites_page, capture_details=True)

    try:
        while (time.time() - start_time) < max_seconds:
            cycle += 1
            cycle_start = time.time()
            loop_monitor.reset_cycle(capture_details=(cycle <= 2))
            print(f"\n[ZARA — Tur #{cycle}] Favoriler yenileniyor...")

            try:
                # Periodically recycle the wishlist page to prevent 6h SPA renderer heap growth.
                needs_recycle = (
                    favorites_page.is_closed()
                    or (cycle > 1 and cycle % PAGE_RECYCLE_INTERVAL_CYCLES == 0)
                )
                if needs_recycle:
                    print(f"[Zara] Bellek temizliği için favori sekmesi yenileniyor (Tur #{cycle})...")
                    favorites_page, loop_monitor = await _recycle_favorites_page(
                        context, favorites_page, loop_monitor, capture_details=False
                    )
                    await safe_goto(favorites_page, ZARA_FAVORITES_URL, timeout_ms=35000, retries=2)
                elif cycle > 1:
                    if not await safe_reload_or_goto(favorites_page, ZARA_FAVORITES_URL, timeout_ms=35000):
                        consecutive_errors += 1
                        print(
                            f"[UYARI] Zara sayfa yenileme başarısız "
                            f"(ardışık hata: {consecutive_errors}/{MAX_CONSECUTIVE_ERRORS})."
                        )
                        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                            send_telegram_message(
                                "🚨 Zara takip botu ardışık bağlantı/proxy hataları nedeniyle durdu."
                            )
                            return False
                        if consecutive_errors % 3 == 0:
                            favorites_page, loop_monitor = await _recycle_favorites_page(
                                context, favorites_page, loop_monitor, capture_details=False
                            )
                        await asyncio.sleep(
                            ERROR_COOLDOWN if consecutive_errors >= 3 else REFRESH_INTERVAL
                        )
                        continue

                await _wait_for_zara_wishlist_ready(
                    favorites_page, monitor=loop_monitor, timeout_ms=10000
                )
                await handle_country_and_cookies(favorites_page)

                if await has_block_or_captcha(favorites_page):
                    consecutive_errors += 1
                    print(
                        f"[UYARI] Zara geçici engeli/CAPTCHA algılandı "
                        f"({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}), {ERROR_COOLDOWN} sn bekleniyor..."
                    )
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        send_telegram_message(
                            "🚨 Zara takip botunda sürekli WAF/CAPTCHA engeli algılandı; çalışma durduruldu."
                        )
                        return False
                    await asyncio.sleep(ERROR_COOLDOWN)
                    continue

                current_url = (favorites_page.url or "").lower()
                if (
                    "logon" in current_url
                    or "login" in current_url
                    or "account.zara.com" in current_url
                    or loop_monitor.has_auth_failure()
                ):
                    print(
                        "[UYARI] Zara oturumu düştü "
                        f"(url={current_url}, auth_err={loop_monitor.auth_failures}), yeniden giriş deneniyor..."
                    )
                    clear_session_valid_marker("zara")
                    if not await ensure_zara_login(context, favorites_page):
                        send_telegram_message(
                            "🚨 Zara oturumu düştü ve yenilenemedi. "
                            "Yerelde giris_zara.bat ile ZARA_STORAGE_STATE secret'ını yenileyin."
                        )
                        return False
                    last_session_save = time.time()
                    consecutive_errors = 0
                    continue

                if "/tr/tr" not in current_url or not any(
                    k in current_url for k in ("wishlist", "lists", "saved")
                ):
                    print("[UYARI] Zara favori sayfası dışına çıkıldığı algılandı, favorilere dönülüyor...")
                    await safe_goto(favorites_page, ZARA_FAVORITES_URL, timeout_ms=35000, retries=2)
                    await _wait_for_zara_wishlist_ready(
                        favorites_page, monitor=loop_monitor, timeout_ms=10000
                    )
                    await handle_country_and_cookies(favorites_page)
                    current_url = (favorites_page.url or "").lower()
                    if (
                        "logon" in current_url
                        or "login" in current_url
                        or "account.zara.com" in current_url
                        or loop_monitor.has_auth_failure()
                    ):
                        print("[UYARI] Zara oturumu düştü, yeniden giriş deneniyor...")
                        clear_session_valid_marker("zara")
                        if not await ensure_zara_login(context, favorites_page):
                            send_telegram_message("🚨 Zara oturumu düştü ve yenilenemedi.")
                            return False
                        last_session_save = time.time()
                        consecutive_errors = 0
                        continue

                await scroll_page_to_bottom(favorites_page)

                extracted = await _extract_stock_and_discounted_items(favorites_page)
                total = extracted.get("total", 0)
                in_stock = extracted.get("in_stock", 0)
                candidates = extracted.get("items", [])
                print(
                    f"[Zara] Favorilerde toplam {total} ürün tarandı "
                    f"(stokta: {in_stock}, stokta+indirimde: {len(candidates)})."
                )
                if cycle in (1, 2) and total == 0:
                    print(f"[Zara] Tur #{cycle} DOM teşhis -> {await _zara_dom_diagnostics(favorites_page)}")
                    print(
                        f"[Zara] Tur #{cycle} Ağ teşhis ({len(loop_monitor.logs)} yanıt) -> {loop_monitor.logs}"
                    )

                if total == 0:
                    loop_state = await _inspect_zara_page(favorites_page)
                    if (
                        loop_state["has_login_url"]
                        or loop_state["has_login_form"]
                        or loop_state["has_login_cta_in_main"]
                        or loop_monitor.has_auth_failure()
                    ):
                        print(
                            "[UYARI] Zara oturumu sayfa içinde düştü "
                            "(login_cta/form/API reddi algılandı), yeniden giriş deneniyor..."
                        )
                        clear_session_valid_marker("zara")
                        if not await ensure_zara_login(context, favorites_page):
                            send_telegram_message(
                                "🚨 Zara oturumu düştü ve yenilenemedi. "
                                "Yerelde giris_zara.bat ile ZARA_STORAGE_STATE secret'ını yenileyin."
                            )
                            return False
                        last_session_save = time.time()
                        consecutive_errors = 0
                        continue

                    if loop_monitor.has_wishlist_server_error():
                        consecutive_errors += 1
                        print(
                            f"[UYARI] Zara wishlist API sunucu hatası ({loop_monitor.api_server_errors}); "
                            "yanlış 'stok tükendi' bildirimi önleniyor."
                        )
                        await asyncio.sleep(REFRESH_INTERVAL)
                        continue

                    consecutive_empty_cycles += 1
                    if had_products_before and consecutive_empty_cycles < 2 and not loop_state["has_empty_wishlist_text"]:
                        print(
                            "[UYARI] Favori listesi geçici olarak 0 ürün döndü; "
                            "yanlış 'stok tükendi' bildirimi atlanıyor, sonraki tur doğrulanacak."
                        )
                        await asyncio.sleep(REFRESH_INTERVAL)
                        continue
                else:
                    had_products_before = True
                    consecutive_empty_cycles = 0

                consecutive_errors = 0

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
                        "[UYARI] Zara tüm ürün sekmeleri hata verdi; yanlış 'Stok Tükendi' "
                        "önlemek için bildirim değerlendirmesi atlandı."
                    )
                else:
                    notifier.process_cycle(matched_items, uncertain_urls=errored_urls)

                now = time.time()
                if (now - last_session_save) >= SESSION_SAVE_INTERVAL_SECONDS and (
                    total > 0 or loop_monitor.wishlist_api_succeeded()
                ):
                    await save_store_session(context, "zara", mark_valid=True)
                    last_session_save = now

            except Exception as exc:
                consecutive_errors += 1
                print(
                    f"[HATA] Zara tur #{cycle} beklenmeyen hata "
                    f"(ardışık: {consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {exc}"
                )
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    send_telegram_message(
                        "🚨 Zara takip botu ardışık hatalar nedeniyle durdu (proxy veya sayfa hatası)."
                    )
                    return False
                if consecutive_errors % 3 == 0 or favorites_page.is_closed():
                    try:
                        favorites_page, loop_monitor = await _recycle_favorites_page(
                            context, favorites_page, loop_monitor, capture_details=False
                        )
                    except Exception as recycle_exc:
                        print(f"[HATA] Zara sekmesi yenilenemedi: {recycle_exc}")
                await asyncio.sleep(
                    ERROR_COOLDOWN if consecutive_errors >= 3 else REFRESH_INTERVAL
                )
                continue

            cycle_elapsed = time.time() - cycle_start
            min_sleep = min(10.0, float(REFRESH_INTERVAL))
            sleep_for = max(min_sleep, float(REFRESH_INTERVAL) - cycle_elapsed)
            remaining = max_seconds - (time.time() - start_time)
            if remaining <= sleep_for:
                break

            print(
                f"[Zara] Tarama tamamlandı ({cycle_elapsed:.1f} sn). "
                f"{sleep_for:.1f} sn bekleniyor..."
            )
            await asyncio.sleep(sleep_for)
    finally:
        loop_monitor.stop()
        try:
            if not favorites_page.is_closed():
                await favorites_page.close()
        except Exception:
            pass

    return True
