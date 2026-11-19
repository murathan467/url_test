import asyncio
import time
from patchright.async_api import BrowserContext, Page
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
    scroll_page_to_bottom,
)
from core.notifier import StockWaveNotifier, send_telegram_message
from core.session import save_store_session


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
        page, country_selectors, timeout_ms=1500, post_wait_ms=1000
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
        page, cookie_selectors, timeout_ms=2000, post_wait_ms=1000
    )
    if matched_cookie:
        print(f"[Mango] Çerezler kabul edildi ({matched_cookie}).")


async def has_block_or_captcha(page: Page) -> bool:
    """Detect WAF/Vercel security checkpoints or CAPTCHA iframes without bypassing them."""
    block_markers = (
        "access denied",
        "attention required",
        "vercel security checkpoint",
        "vercel güvenlik kontrol noktası",
        "tarayıcınız doğrulanamadı",
        "failed to verify your browser",
    )
    try:
        title = (await page.title() or "").lower()
        if any(k in title for k in block_markers):
            return True
    except Exception:
        pass
    try:
        text = (await page.locator("body").inner_text(timeout=2500)).lower()
        if any(k in text for k in block_markers):
            return True
        captcha_count = await page.locator(
            "iframe[src*='captcha'], iframe[src*='arkose'], iframe[title*='reCAPTCHA'], #sec-overlay"
        ).count()
        if captcha_count > 0:
            return True
    except Exception:
        pass
    return False


async def _inspect_mango_page(page: Page) -> dict:
    """Inspect current Mango page URL, product links, login form, and main login CTA state."""
    has_login_form = False
    try:
        has_login_form = await page.locator(
            "input[id='login-form-email-input'], input[type='email'], input[type='password']"
        ).first.is_visible(timeout=1500)
    except Exception:
        pass

    info = await page.evaluate(
        """
        () => {
            const url = (window.location.href || "").toLowerCase();
            const mainLinks = document.querySelectorAll('main a[href*="/p/"], a[href*="/p/"]');
            const seen = new Set();
            mainLinks.forEach(a => {
                if (a.href) seen.add(a.href);
            });
            const mainEl = document.querySelector('main, [role="main"]') || document.body;
            const mainText = mainEl ? (mainEl.innerText || "").toLowerCase() : "";
            const hasCtaWords = ["giriş yap", "oturum aç", "sign in", "log in"].some(k => mainText.includes(k));
            return {
                is_tr_store: url.includes("/tr/"),
                is_favorites_url: ["favorites", "favori", "wishlist", "saved"].some(k => url.includes(k)),
                has_login_url: url.includes("login") || url.includes("signin"),
                total_product_links: seen.size,
                has_login_cta_in_main: seen.size === 0 && hasCtaWords
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


async def ensure_mango_login(context: BrowserContext, page: Page) -> bool:
    """Verify Mango session on favorites or perform login."""
    try:
        await safe_goto(page, MANGO_FAVORITES_URL, timeout_ms=30000, retries=2)
        await page.wait_for_timeout(4500)
        await handle_country_and_cookies(page)

        if "/tr/" not in (page.url or "").lower():
            await safe_goto(page, MANGO_FAVORITES_URL, timeout_ms=30000, retries=2)
            await page.wait_for_timeout(4000)
            await handle_country_and_cookies(page)

        diag = await page_diagnostics(page)
        state = await _inspect_mango_page(page)
        print(
            f"[Mango] İlk sayfa kontrolü -> {diag} | tr={state['is_tr_store']} "
            f"fav={state['is_favorites_url']} urun_link={state['total_product_links']} "
            f"login_form={state['has_login_form']} login_cta={state['has_login_cta_in_main']}"
        )

        if await _is_mango_session_active(page, state):
            print("[Mango] ✅ Kayıtlı oturum aktif, doğrudan favorilere girildi.")
            await save_store_session(context, "mango", mark_valid=True)
            return True
    except Exception as exc:
        print(f"[HATA] Mango başlangıç oturum kontrolü hatası: {exc}")
        return False

    if not COMMON_EMAIL or not COMMON_PASSWORD:
        print("[HATA] COMMON_EMAIL veya COMMON_PASSWORD eksik.")
        return False

    print("[Mango] Oturum kapalı, otomatik giriş başlatılıyor...")
    try:
        await safe_goto(page, MANGO_LOGIN_URL, timeout_ms=30000, retries=2)
        await page.wait_for_timeout(3000)
        await handle_country_and_cookies(page)

        if await has_block_or_captcha(page):
            print("[HATA] Mango giriş sayfasında Captcha/WAF/Vercel engeli çıktı.")
            return False

        email_input = page.locator("input[id='login-form-email-input'], input[type='email']").first
        if not await email_input.is_visible(timeout=3000):
            login_icon = page.locator(
                "a:has-text('Giriş yap'), button:has-text('Giriş yap'), a:has-text('Giriş')"
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

        pass_input = page.locator("input[id='login-form-password-input'], input[type='password']").first
        await pass_input.wait_for(state="visible", timeout=15000)
        await human_type(pass_input, COMMON_PASSWORD)
        await page.wait_for_timeout(800)

        await page.locator(
            "button[id='login-form-submit-button'], button[type='submit']"
        ).first.click()
        await page.wait_for_timeout(6000)

        await safe_goto(page, MANGO_FAVORITES_URL, timeout_ms=30000, retries=2)
        await page.wait_for_timeout(4000)
        await handle_country_and_cookies(page)

        if "/tr/" not in (page.url or "").lower():
            await safe_goto(page, MANGO_FAVORITES_URL, timeout_ms=30000, retries=2)
            await page.wait_for_timeout(4000)
            await handle_country_and_cookies(page)

        post_state = await _inspect_mango_page(page)
        if not await _is_mango_session_active(page, post_state):
            print(f"[HATA] Mango girişi tamamlanamadı -> {await page_diagnostics(page)}")
            return False

        print("[Mango] ✅ Otomatik giriş başarılı!")
        await save_store_session(context, "mango", mark_valid=True)
        return True
    except Exception as exc:
        print(f"[HATA] Mango login hatası: {exc}")
        return False


async def _extract_mango_candidates(page: Page) -> dict:
    """Extract Mango favorite totals and in-stock candidate items."""
    return await page.evaluate(
        """
        () => {
            const results = [];
            const seenUrls = new Set();

            const extractCard = (card, linkEl) => {
                if (!card || !linkEl) return;
                const href = linkEl.href;
                if (!href || seenUrls.has(href)) return;
                seenUrls.add(href);

                const cardText = (card.innerText || "").toUpperCase();
                if (
                    cardText.includes("TÜKENDİ") ||
                    cardText.includes("OUT OF STOCK") ||
                    cardText.includes("MEVCUT DEĞİL")
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

            return { total: seenUrls.size, items: results };
        }
        """
    )


async def _open_and_check_tab(
    context: BrowserContext, product: dict, sem: asyncio.Semaphore
) -> dict | None:
    """Open Mango product in a new tab (bounded by sem); require BOTH discount and TARGET_SIZES stock."""
    target_upper = [str(s).upper() for s in TARGET_SIZES]
    async with sem:
        tab = await context.new_page()
        try:
            await safe_goto(tab, product["url"], timeout_ms=30000, retries=2)
            await tab.wait_for_timeout(2500)

            info = await tab.evaluate(
                """
                (targetSizes) => {
                    const info = { sizes: [], oldPrice: "", newPrice: "", hasDiscount: false, name: "" };

                    try {
                        const titleEl = document.querySelector('h1[class*="ProductDetail_title"], h1');
                        if (titleEl) info.name = titleEl.innerText.trim();

                        const allSrOnly = document.querySelectorAll('div[class*="Price_container"] span[class*="srOnly"]');
                        allSrOnly.forEach(span => {
                            const txt = span.innerText || "";
                            if (txt.includes("ilk fiyat") || txt.includes("Üstü çizili")) {
                                const match = txt.match(/\\[(.*?)\\]/);
                                if (match) info.oldPrice = match[1].trim();
                            }
                            if (txt.includes("Güncel fiyat") || txt.includes("Geçerli fiyat")) {
                                const match = txt.match(/\\[(.*?)\\]/);
                                if (match) info.newPrice = match[1].trim();
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

                        const isOutOfStock =
                            (btn.className && btn.className.includes("Unavailable")) ||
                            (btn.id && btn.id.includes("Unavailable")) ||
                            btn.hasAttribute("disabled") ||
                            btn.querySelector('span[class*="notAvailable"]') !== null ||
                            (btn.querySelector('span[class*="SizeItemContent_meta"]') !== null && btn.innerText.includes("değil"));

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
                return item
            return None
        except Exception as exc:
            print(f"  [HATA] Mango sekme tarama hatası ({product.get('name', 'Ürün')}): {exc}")
            return None
        finally:
            try:
                if not tab.is_closed():
                    await tab.close()
            except Exception:
                pass


async def run_mango_loop(context: BrowserContext, max_run_minutes: int) -> bool:
    """Run Mango favorites polling loop with bounded concurrent discount + size checks."""
    favorites_page = await context.new_page()
    if not await ensure_mango_login(context, favorites_page):
        send_telegram_message("🚨 Mango oturumu açılamadı (Captcha/WAF/Vercel engeli).")
        return False

    notifier = StockWaveNotifier("MANGO")
    start_time = time.time()
    max_seconds = max_run_minutes * 60 if max_run_minutes > 0 else float("inf")
    cycle = 0
    sem = asyncio.Semaphore(4)

    while (time.time() - start_time) < max_seconds:
        cycle += 1
        print(f"\n[MANGO — Tur #{cycle}] Favoriler yenileniyor...")

        if cycle > 1:
            if not await safe_reload_or_goto(favorites_page, MANGO_FAVORITES_URL, timeout_ms=30000):
                await asyncio.sleep(REFRESH_INTERVAL)
                continue
            await favorites_page.wait_for_timeout(3500)

        await handle_country_and_cookies(favorites_page)

        if await has_block_or_captcha(favorites_page):
            print(f"[UYARI] Mango geçici engeli algılandı, {ERROR_COOLDOWN} sn bekleniyor...")
            await asyncio.sleep(ERROR_COOLDOWN)
            continue

        current_url = (favorites_page.url or "").lower()
        if "login" in current_url or "signin" in current_url:
            print("[UYARI] Mango oturumu düştü, yeniden giriş deneniyor...")
            if not await ensure_mango_login(context, favorites_page):
                send_telegram_message("🚨 Mango oturumu düştü ve yenilenemedi.")
                return False

        if "/tr/" not in current_url or not any(
            k in current_url for k in ("favorites", "favori", "wishlist", "saved")
        ):
            print("[UYARI] Mango favori sayfası dışına çıkıldığı algılandı, favorilere dönülüyor...")
            try:
                await safe_goto(favorites_page, MANGO_FAVORITES_URL, timeout_ms=30000, retries=2)
                await favorites_page.wait_for_timeout(4000)
                await handle_country_and_cookies(favorites_page)
            except Exception as exc:
                print(f"[HATA] Mango favorilere dönülemedi: {exc}")
                await asyncio.sleep(REFRESH_INTERVAL)
                continue
            current_url = (favorites_page.url or "").lower()
            if "login" in current_url or "signin" in current_url:
                print("[UYARI] Mango oturumu düştü, yeniden giriş deneniyor...")
                if not await ensure_mango_login(context, favorites_page):
                    send_telegram_message("🚨 Mango oturumu düştü ve yenilenemedi.")
                    return False

        await scroll_page_to_bottom(favorites_page)

        extracted = await _extract_mango_candidates(favorites_page)
        total = extracted.get("total", 0)
        candidates = extracted.get("items", [])
        print(f"[Mango] Favorilerde toplam {total} ürün görüldü ({len(candidates)} stokta aday).")
        if cycle == 1 and total == 0:
            print(f"[Mango] İlk tur 0 ürün teşhisi -> {await page_diagnostics(favorites_page)}")

        matched_items: list[dict] = []
        if candidates:
            results = await asyncio.gather(
                *[_open_and_check_tab(context, prod, sem) for prod in candidates]
            )
            matched_items = [r for r in results if r is not None]

        notifier.process_cycle(matched_items)
        await save_store_session(context, "mango", mark_valid=True)

        remaining = max_seconds - (time.time() - start_time)
        if remaining <= REFRESH_INTERVAL:
            break

        print(f"[Mango] Tarama tamamlandı. {REFRESH_INTERVAL} sn bekleniyor...")
        await asyncio.sleep(REFRESH_INTERVAL)

    return True
