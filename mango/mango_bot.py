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
from core.browser import click_first_visible, human_type, scroll_page_to_bottom
from core.notifier import StockWaveNotifier, send_telegram_message
from core.session import save_store_session


async def handle_country_and_cookies(page: Page) -> None:
    """Close Mango cookie banner and any country confirmation modal."""
    country_selectors = [
        "[role='dialog'] button:has-text('Türkiye')",
        "[role='dialog'] button:has-text('Turkey')",
        "[role='dialog'] button:has-text('Devam et')",
        "[role='dialog'] button:has-text('Continue')",
        "[class*='CountrySelector'] button:has-text('Türkiye')",
    ]
    if await click_first_visible(page, country_selectors, timeout_ms=1500, post_wait_ms=1000):
        print("[Mango] Ülke seçimi onaylandı.")

    cookie_selectors = [
        "button[id='cookies.button.acceptAll']",
        "#onetrust-accept-btn-handler",
        "button:has-text('Tümünü kabul et')",
        "button:has-text('TÜMÜNÜ KABUL ET')",
        "button:has-text('Accept all')",
    ]
    if await click_first_visible(page, cookie_selectors, timeout_ms=2000, post_wait_ms=1000):
        print("[Mango] Çerezler kabul edildi.")


async def has_block_or_captcha(page: Page) -> bool:
    """Detect WAF blocks or CAPTCHA iframes without bypassing them."""
    try:
        text = (await page.locator("body").inner_text(timeout=2500)).lower()
        if any(k in text for k in ["access denied", "attention required"]):
            return True
        captcha_count = await page.locator(
            "iframe[src*='captcha'], iframe[src*='arkose'], iframe[title*='reCAPTCHA'], #sec-overlay"
        ).count()
        if captcha_count > 0:
            return True
    except Exception:
        pass
    return False


async def ensure_mango_login(context: BrowserContext, page: Page) -> bool:
    """Verify Mango session on favorites or perform login."""
    await page.goto(MANGO_FAVORITES_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3500)
    await handle_country_and_cookies(page)

    email_field = page.locator("input[id='login-form-email-input']").first
    has_login_form = False
    try:
        has_login_form = await email_field.is_visible(timeout=1500)
    except Exception:
        pass

    if "login" not in page.url.lower() and not has_login_form and not await has_block_or_captcha(page):
        print("[Mango] ✅ Kayıtlı oturum aktif, doğrudan favorilere girildi.")
        await save_store_session(context, "mango")
        return True

    if not COMMON_EMAIL or not COMMON_PASSWORD:
        print("[HATA] COMMON_EMAIL veya COMMON_PASSWORD eksik.")
        return False

    print("[Mango] Oturum kapalı, otomatik giriş başlatılıyor...")
    try:
        await page.goto(MANGO_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        await handle_country_and_cookies(page)

        if await has_block_or_captcha(page):
            print("[HATA] Mango giriş sayfasında Captcha/WAF engeli çıktı.")
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

        await page.goto(MANGO_FAVORITES_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3500)
        await handle_country_and_cookies(page)

        if "login" in page.url.lower() or await has_block_or_captcha(page):
            print("[HATA] Mango girişi tamamlanamadı.")
            return False

        print("[Mango] ✅ Otomatik giriş başarılı!")
        await save_store_session(context, "mango")
        return True
    except Exception as exc:
        print(f"[HATA] Mango login hatası: {exc}")
        return False


async def _extract_mango_candidates(page: Page) -> list[dict]:
    """Extract Mango favorite items, excluding cards explicitly marked out of stock."""
    return await page.evaluate(
        """
        () => {
            const results = [];
            const seenUrls = new Set();
            const cards = document.querySelectorAll('.virtual-item, [class*="ProductCard_productCard"]');

            cards.forEach(card => {
                const cardText = (card.innerText || "").toUpperCase();
                if (
                    cardText.includes("TÜKENDİ") ||
                    cardText.includes("OUT OF STOCK") ||
                    cardText.includes("MEVCUT DEĞİL")
                ) {
                    return;
                }

                let linkEl = card.querySelector('a[class*="ProductImage_imageWrapper"], a[href]');
                if (!linkEl) linkEl = card.querySelector('a');
                if (!linkEl) return;

                const href = linkEl.href;
                if (!href || seenUrls.has(href)) return;
                seenUrls.add(href);

                const nameEl = card.querySelector('[class*="ProductTitle"], [class*="title"], h2, h3');
                const name = nameEl ? nameEl.innerText.trim() : (linkEl.getAttribute("aria-label") || "Mango Ürün");

                results.push({
                    name: name || "Mango Ürün",
                    url: href,
                    oldPrice: "",
                    newPrice: "",
                    hasDiscount: false
                });
            });

            return results;
        }
        """
    )


async def _open_and_check_tab(context: BrowserContext, product: dict) -> dict | None:
    """Open Mango product in a new tab concurrently; require BOTH discount and TARGET_SIZES stock."""
    target_upper = [str(s).upper() for s in TARGET_SIZES]
    tab = await context.new_page()
    try:
        await tab.goto(product["url"], wait_until="domcontentloaded", timeout=30000)
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


async def run_mango_loop(context: BrowserContext, max_run_minutes: int) -> None:
    """Run Mango favorites polling loop with simultaneous multi-tab discount + size checks."""
    favorites_page = await context.new_page()
    if not await ensure_mango_login(context, favorites_page):
        send_telegram_message("🚨 Mango oturumu açılamadı (Captcha/Login engeli).")
        return

    notifier = StockWaveNotifier("MANGO")
    start_time = time.time()
    max_seconds = max_run_minutes * 60 if max_run_minutes > 0 else float("inf")
    cycle = 0

    while (time.time() - start_time) < max_seconds:
        cycle += 1
        print(f"\n[MANGO — Tur #{cycle}] Favoriler yenileniyor...")

        if cycle > 1:
            try:
                await favorites_page.reload(wait_until="domcontentloaded", timeout=30000)
                await favorites_page.wait_for_timeout(3500)
            except Exception as exc:
                print(f"[HATA] Mango sayfası yenilenemedi: {exc}")
                await asyncio.sleep(REFRESH_INTERVAL)
                continue

        await handle_country_and_cookies(favorites_page)

        if await has_block_or_captcha(favorites_page):
            print(f"[UYARI] Mango geçici engeli algılandı, {ERROR_COOLDOWN} sn bekleniyor...")
            await asyncio.sleep(ERROR_COOLDOWN)
            continue

        if "login" in favorites_page.url.lower():
            print("[UYARI] Mango oturumu düştü, yeniden giriş deneniyor...")
            if not await ensure_mango_login(context, favorites_page):
                send_telegram_message("🚨 Mango oturumu düştü ve yenilenemedi.")
                return

        await scroll_page_to_bottom(favorites_page)

        candidates = await _extract_mango_candidates(favorites_page)
        print(
            f"[Mango] {len(candidates)} aday ürün bulundu. "
            "Stok+indirim kontrolü için tüm yan sekmeler eşzamanlı açılıyor..."
        )

        matched_items: list[dict] = []
        if candidates:
            results = await asyncio.gather(
                *[_open_and_check_tab(context, prod) for prod in candidates]
            )
            matched_items = [r for r in results if r is not None]

        notifier.process_cycle(matched_items)
        await save_store_session(context, "mango")

        remaining = max_seconds - (time.time() - start_time)
        if remaining <= REFRESH_INTERVAL:
            break

        print(f"[Mango] Tarama tamamlandı. {REFRESH_INTERVAL} sn bekleniyor...")
        await asyncio.sleep(REFRESH_INTERVAL)
