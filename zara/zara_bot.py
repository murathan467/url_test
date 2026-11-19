import asyncio
import json
import time
from collections.abc import Callable
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
from core.session import save_store_session


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
    try:
        text = (await page.locator("body").inner_text(timeout=2500)).lower()
        if any(k in text for k in ["bir sorun oluştu", "access denied"]):
            return True
        captcha_count = await page.locator(
            "iframe[src*='captcha'], iframe[src*='arkose'], #sec-overlay"
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
            "input[name='email'], input[name='logonId'], input[name='password'], "
            "input[type='email'], input[type='password']"
        ).first.is_visible(timeout=500)
    except Exception:
        pass

    info = await page.evaluate(
        """
        () => {
            const url = (window.location.href || "").toLowerCase();
            const links = document.querySelectorAll('a[href*="-p"]');
            const seen = new Set();
            links.forEach(a => {
                if (a.href && /-p\\d{4,}\\.html($|\\?)/.test(a.href)) {
                    seen.add(a.href);
                }
            });
            const mainEl = document.querySelector('main, [role="main"]') || document.body;
            const mainText = mainEl ? (mainEl.innerText || "").toLowerCase() : "";
            const hasCtaWords = ["giriş yap", "oturum aç", "log in", "sign in"].some(k => mainText.includes(k));
            return {
                is_tr_store: url.includes("/tr/tr"),
                is_wishlist_url: ["wishlist", "lists", "saved"].some(k => url.includes(k)),
                has_login_url: url.includes("logon") || url.includes("login"),
                total_product_links: seen.size,
                has_login_cta_in_main: seen.size === 0 && hasCtaWords
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


def _attach_zara_network_logger(page: Page) -> tuple[list[str], Callable[[], None]]:
    """Attach a bounded, secret-free response logger for Zara wishlist/API calls."""
    logs: list[str] = []
    state = {"active": True}
    static_exts = (".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".woff", ".woff2", ".ico", ".map")
    interesting = ("wishlist", "itxrest", "/lists", "saved", "favorite")

    async def _on_response(resp: Response) -> None:
        if not state["active"] or len(logs) >= 25:
            return
        try:
            path = resp.url.split("?")[0]
            lower_path = path.lower()
            if lower_path.endswith(static_exts):
                return
            if not any(tok in lower_path for tok in interesting):
                return

            ct = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
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


async def ensure_zara_login(context: BrowserContext, page: Page) -> bool:
    """Verify Zara session on wishlist or perform login via home -> logon."""
    net_logs, stop_net_logger = _attach_zara_network_logger(page)
    try:
        await safe_goto(page, ZARA_FAVORITES_URL, timeout_ms=30000, retries=2)
        await page.wait_for_timeout(4000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await handle_country_and_cookies(page)

        if "/tr/tr" not in (page.url or "").lower():
            await safe_goto(page, ZARA_FAVORITES_URL, timeout_ms=30000, retries=2)
            await page.wait_for_timeout(3500)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await handle_country_and_cookies(page)

        diag = await page_diagnostics(page)
        state = await _inspect_zara_page(page)
        print(
            f"[Zara] İlk sayfa kontrolü -> {diag} | tr={state['is_tr_store']} "
            f"wishlist={state['is_wishlist_url']} urun_link={state['total_product_links']} "
            f"login_form={state['has_login_form']} login_cta={state['has_login_cta_in_main']}"
        )
        if state["total_product_links"] == 0:
            print(f"[Zara] DOM teşhis -> {await _zara_dom_diagnostics(page)}")
            print(f"[Zara] Ağ teşhis ({len(net_logs)} yanıt) -> {net_logs}")

        if await _is_zara_session_active(page, state):
            print("[Zara] ✅ Kayıtlı oturum aktif, doğrudan favorilere girildi.")
            await save_store_session(context, "zara", mark_valid=True)
            return True
    except Exception as exc:
        print(f"[HATA] Zara başlangıç oturum kontrolü hatası: {exc}")
        return False
    finally:
        stop_net_logger()

    if not COMMON_EMAIL or not COMMON_PASSWORD:
        print("[HATA] COMMON_EMAIL veya COMMON_PASSWORD eksik.")
        return False

    print("[Zara] Oturum kapalı, otomatik giriş başlatılıyor...")
    try:
        await safe_goto(page, ZARA_HOME_URL, timeout_ms=30000, retries=2)
        await page.wait_for_timeout(3000)
        await handle_country_and_cookies(page)

        login_link = page.locator("a[href*='logon'], a:has-text('GİRİŞ YAP')").first
        try:
            if await login_link.is_visible(timeout=4000):
                await login_link.click()
            else:
                await safe_goto(page, ZARA_LOGIN_URL, timeout_ms=30000, retries=2)
        except Exception:
            await safe_goto(page, ZARA_LOGIN_URL, timeout_ms=30000, retries=2)

        await page.wait_for_timeout(3500)
        await handle_country_and_cookies(page)

        if await has_block_or_captcha(page):
            print("[HATA] Zara giriş sayfasında Captcha/WAF engeli çıktı.")
            return False

        email_input = page.locator(
            "input[name='email'], input[name='logonId'], input[type='email'], input[aria-label*='E-posta']"
        ).first
        await email_input.wait_for(state="visible", timeout=15000)
        await human_type(email_input, COMMON_EMAIL)
        await page.wait_for_timeout(800)

        await page.locator(
            "button[data-qa-action='submit'], button.submit-button, button[type='submit']"
        ).first.click()
        await page.wait_for_timeout(5000)
        await handle_country_and_cookies(page)

        if await has_block_or_captcha(page):
            print("[HATA] E-posta adımında Captcha veya 'Bir sorun oluştu' tetiklendi.")
            return False

        pwd_link = page.get_by_text("Şifre ile giriş yap").or_(
            page.get_by_text("Log in with password")
        ).first
        try:
            if await pwd_link.is_visible(timeout=3000):
                await pwd_link.click()
                await page.wait_for_timeout(1500)
        except Exception:
            pass

        pass_input = page.locator(
            "input[name='password'], input[type='password'], input[aria-label*='Parola']"
        ).first
        await pass_input.wait_for(state="visible", timeout=15000)
        await human_type(pass_input, COMMON_PASSWORD)
        await page.wait_for_timeout(800)

        await page.locator(
            "button[data-qa-action='submit'], button.submit-button, button[type='submit']"
        ).first.click()
        await page.wait_for_timeout(6000)

        await safe_goto(page, ZARA_FAVORITES_URL, timeout_ms=30000, retries=2)
        await page.wait_for_timeout(3500)
        await handle_country_and_cookies(page)

        if "/tr/tr" not in (page.url or "").lower():
            await safe_goto(page, ZARA_FAVORITES_URL, timeout_ms=30000, retries=2)
            await page.wait_for_timeout(3500)
            await handle_country_and_cookies(page)

        post_state = await _inspect_zara_page(page)
        if not await _is_zara_session_active(page, post_state):
            print(f"[HATA] Zara girişi tamamlanamadı -> {await page_diagnostics(page)}")
            return False

        print("[Zara] ✅ Otomatik giriş başarılı!")
        await save_store_session(context, "zara", mark_valid=True)
        return True
    except Exception as exc:
        print(f"[HATA] Zara login hatası: {exc}")
        return False


async def _extract_stock_and_discounted_items(page: Page) -> dict:
    """Return Zara wishlist totals and items that appear both in stock and discounted."""
    return await page.evaluate(
        """
        () => {
            const results = [];
            const seenUrls = new Set();
            let inStockCount = 0;
            const links = document.querySelectorAll('a[href*="-p"]');

            links.forEach(link => {
                const href = link.href;
                if (!href.match(/-p\\d{4,}\\.html($|\\?)/)) return;
                if (seenUrls.has(href)) return;
                seenUrls.add(href);

                const card = link.closest('li, article, .product-grid-product-info, .product-grid-product, .product-detail-info')
                             || link.parentElement?.parentElement?.parentElement;
                if (!card) return;

                const nameEl = card.querySelector('[data-qa-qualifier="product-detail-secondary-product-info-name"]');
                const name = nameEl ? nameEl.innerText.trim() : link.innerText.trim();
                if (!name) return;

                const cardText = card.innerText.toUpperCase();
                const hasStock = !(
                    cardText.includes("TÜKENDİ") ||
                    cardText.includes("OUT OF STOCK") ||
                    cardText.includes("BENZER ÜRÜNLER") ||
                    cardText.includes("COMING SOON")
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
                    results.push({ name, url: href, oldPrice, newPrice, hasDiscount: true });
                }
            });

            return { total: seenUrls.size, in_stock: inStockCount, items: results };
        }
        """
    )


async def _open_and_check_tab(
    context: BrowserContext, product: dict, sem: asyncio.Semaphore
) -> dict | None:
    """Open product page in a new tab (bounded by sem) and check TARGET_SIZES availability."""
    target_upper = [str(s).upper() for s in TARGET_SIZES]
    async with sem:
        tab = await context.new_page()
        try:
            await safe_goto(tab, product["url"], timeout_ms=30000, retries=2)
            await tab.wait_for_timeout(2200)

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
                    const found = [];
                    const buttons = document.querySelectorAll(
                        'button.size-selector-sizes-size__button, li.size-selector-list__item, button[data-qa-action*="size"]'
                    );
                    buttons.forEach(btn => {
                        const txt = btn.innerText.trim().toUpperCase();
                        const isOut = txt.includes("TÜKENDİ") || txt.includes("BENZER") ||
                                      txt.includes("COMING SOON") || btn.hasAttribute("disabled") ||
                                      btn.getAttribute("data-qa-action") === "size-out-of-stock" ||
                                      (btn.className && btn.className.includes("out-of-stock"));
                        if (!isOut) {
                            let sizeName = txt.split("\\n")[0].trim();
                            const lbl = btn.querySelector(".size-selector-sizes-size__label-content");
                            if (lbl) sizeName = lbl.innerText.trim().toUpperCase();

                            for (const t of targetSizes) {
                                if (new RegExp("\\\\b" + t + "\\\\b", "i").test(sizeName)) {
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
                return item
            return None
        except Exception as exc:
            print(f"  [HATA] Sekme tarama hatası ({product.get('name', 'Ürün')}): {exc}")
            return None
        finally:
            try:
                if not tab.is_closed():
                    await tab.close()
            except Exception:
                pass


async def run_zara_loop(context: BrowserContext, max_run_minutes: int) -> bool:
    """Run Zara wishlist polling loop with bounded concurrent size verification."""
    favorites_page = await context.new_page()
    if not await ensure_zara_login(context, favorites_page):
        send_telegram_message("🚨 Zara oturumu açılamadı (Ülke/Captcha/Login engeli).")
        return False

    notifier = StockWaveNotifier("ZARA")
    start_time = time.time()
    max_seconds = max_run_minutes * 60 if max_run_minutes > 0 else float("inf")
    cycle = 0
    sem = asyncio.Semaphore(4)
    loop_net_logs, stop_loop_net_logger = _attach_zara_network_logger(favorites_page)

    try:
        while (time.time() - start_time) < max_seconds:
            cycle += 1
            if cycle <= 2:
                loop_net_logs.clear()
            print(f"\n[ZARA — Tur #{cycle}] Favoriler yenileniyor...")

            if cycle > 1:
                if not await safe_reload_or_goto(favorites_page, ZARA_FAVORITES_URL, timeout_ms=30000):
                    await asyncio.sleep(REFRESH_INTERVAL)
                    continue
                await favorites_page.wait_for_timeout(3500)
                try:
                    await favorites_page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass

            await handle_country_and_cookies(favorites_page)

            if await has_block_or_captcha(favorites_page):
                print(f"[UYARI] Zara geçici engeli algılandı, {ERROR_COOLDOWN} sn bekleniyor...")
                await asyncio.sleep(ERROR_COOLDOWN)
                continue

            current_url = (favorites_page.url or "").lower()
            if "logon" in current_url or "login" in current_url:
                print("[UYARI] Zara oturumu düştü, yeniden giriş deneniyor...")
                if not await ensure_zara_login(context, favorites_page):
                    send_telegram_message("🚨 Zara oturumu düştü ve yenilenemedi.")
                    return False

            if "/tr/tr" not in current_url or not any(
                k in current_url for k in ("wishlist", "lists", "saved")
            ):
                print("[UYARI] Zara favori sayfası dışına çıkıldığı algılandı, favorilere dönülüyor...")
                try:
                    await safe_goto(favorites_page, ZARA_FAVORITES_URL, timeout_ms=30000, retries=2)
                    await favorites_page.wait_for_timeout(3500)
                    await handle_country_and_cookies(favorites_page)
                except Exception as exc:
                    print(f"[HATA] Zara favorilere dönülemedi: {exc}")
                    await asyncio.sleep(REFRESH_INTERVAL)
                    continue
                current_url = (favorites_page.url or "").lower()
                if "logon" in current_url or "login" in current_url:
                    print("[UYARI] Zara oturumu düştü, yeniden giriş deneniyor...")
                    if not await ensure_zara_login(context, favorites_page):
                        send_telegram_message("🚨 Zara oturumu düştü ve yenilenemedi.")
                        return False

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
                print(f"[Zara] Tur #{cycle} Ağ teşhis ({len(loop_net_logs)} yanıt) -> {loop_net_logs}")
            if cycle >= 2:
                stop_loop_net_logger()

            matched_items: list[dict] = []
            if candidates:
                results = await asyncio.gather(
                    *[_open_and_check_tab(context, prod, sem) for prod in candidates]
                )
                matched_items = [r for r in results if r is not None]

            notifier.process_cycle(matched_items)
            await save_store_session(context, "zara", mark_valid=True)

            remaining = max_seconds - (time.time() - start_time)
            if remaining <= REFRESH_INTERVAL:
                break

            print(f"[Zara] Tarama tamamlandı. {REFRESH_INTERVAL} sn bekleniyor...")
            await asyncio.sleep(REFRESH_INTERVAL)
    finally:
        stop_loop_net_logger()

    return True
