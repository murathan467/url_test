import asyncio
import time
from patchright.async_api import BrowserContext, Page
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
from core.browser import click_first_visible, human_type, scroll_page_to_bottom
from core.notifier import StockWaveNotifier, send_telegram_message
from core.session import save_store_session


async def handle_country_and_cookies(page: Page) -> None:
    """Close Zara country selection modal, cookie banner, and error dialogs."""
    country_selectors = [
        "button[data-qa-action='stay-in-store']",
        "button[data-qa-action='go-to-store']",
        "button:has-text('Türkiye')",
        "button:has-text('TÜRKİYE')",
        "button:has-text('DEVAM ET')",
        "button:has-text('Continue to Turkey')",
    ]
    if await click_first_visible(page, country_selectors, timeout_ms=2000, post_wait_ms=1500):
        print("[Zara] Türkiye mağaza seçimi onaylandı.")

    cookie_selectors = [
        "#onetrust-accept-btn-handler",
        "button[data-qa-action='accept-all']",
        "button:has-text('TÜM ÇEREZLERİ KABUL ET')",
        "button:has-text('Tüm çerezleri kabul et')",
        "button:has-text('ACCEPT ALL')",
    ]
    if await click_first_visible(page, cookie_selectors, timeout_ms=2000, post_wait_ms=1000):
        print("[Zara] Çerezler kabul edildi.")

    error_dialog_selectors = [
        "[role='dialog'] button:has-text('OK')",
        "[role='dialog'] button:has-text('Ok')",
        "[role='dialog'] button:has-text('Tamam')",
        "[role='dialog'] button:has-text('TAMAM')",
    ]
    if await click_first_visible(page, error_dialog_selectors, timeout_ms=1000, post_wait_ms=1000):
        print("[Zara] Hata diyaloğu kapatıldı.")


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


async def ensure_zara_login(context: BrowserContext, page: Page) -> bool:
    """Verify Zara session on wishlist or perform login via home -> logon."""
    await page.goto(ZARA_FAVORITES_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3500)
    await handle_country_and_cookies(page)

    current_url = page.url.lower()
    if "logon" not in current_url and "login" not in current_url and not await has_block_or_captcha(page):
        print("[Zara] ✅ Kayıtlı oturum aktif, doğrudan favorilere girildi.")
        await save_store_session(context, "zara")
        return True

    if not COMMON_EMAIL or not COMMON_PASSWORD:
        print("[HATA] COMMON_EMAIL veya COMMON_PASSWORD eksik.")
        return False

    print("[Zara] Oturum kapalı, otomatik giriş başlatılıyor...")
    try:
        await page.goto(ZARA_HOME_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        await handle_country_and_cookies(page)

        login_link = page.locator("a[href*='logon'], a:has-text('GİRİŞ YAP')").first
        try:
            if await login_link.is_visible(timeout=4000):
                await login_link.click()
            else:
                await page.goto(ZARA_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            await page.goto(ZARA_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

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

        await page.goto(ZARA_FAVORITES_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3500)
        await handle_country_and_cookies(page)

        current_url = page.url.lower()
        if "logon" in current_url or "login" in current_url or await has_block_or_captcha(page):
            print("[HATA] Zara girişi tamamlanamadı.")
            return False

        print("[Zara] ✅ Otomatik giriş başarılı!")
        await save_store_session(context, "zara")
        return True
    except Exception as exc:
        print(f"[HATA] Zara login hatası: {exc}")
        return False


async def _extract_stock_and_discounted_items(page: Page) -> list[dict]:
    """Return Zara wishlist items that appear both in stock and discounted."""
    return await page.evaluate(
        """
        () => {
            const results = [];
            const seenUrls = new Set();
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

            return results;
        }
        """
    )


async def _open_and_check_tab(context: BrowserContext, product: dict) -> dict | None:
    """Open product page in a new tab concurrently and check TARGET_SIZES availability."""
    target_upper = [str(s).upper() for s in TARGET_SIZES]
    tab = await context.new_page()
    try:
        await tab.goto(product["url"], wait_until="domcontentloaded", timeout=30000)
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


async def run_zara_loop(context: BrowserContext, max_run_minutes: int) -> None:
    """Run Zara wishlist polling loop with simultaneous multi-tab size verification."""
    favorites_page = await context.new_page()
    if not await ensure_zara_login(context, favorites_page):
        send_telegram_message("🚨 Zara oturumu açılamadı (Ülke/Captcha/Login engeli).")
        return

    notifier = StockWaveNotifier("ZARA")
    start_time = time.time()
    max_seconds = max_run_minutes * 60 if max_run_minutes > 0 else float("inf")
    cycle = 0

    while (time.time() - start_time) < max_seconds:
        cycle += 1
        print(f"\n[ZARA — Tur #{cycle}] Favoriler yenileniyor...")

        if cycle > 1:
            try:
                await favorites_page.reload(wait_until="domcontentloaded", timeout=30000)
                await favorites_page.wait_for_timeout(3500)
            except Exception as exc:
                print(f"[HATA] Sayfa yenilenemedi: {exc}")
                await asyncio.sleep(REFRESH_INTERVAL)
                continue

        await handle_country_and_cookies(favorites_page)

        if await has_block_or_captcha(favorites_page):
            print(f"[UYARI] Zara geçici engeli algılandı, {ERROR_COOLDOWN} sn bekleniyor...")
            await asyncio.sleep(ERROR_COOLDOWN)
            continue

        current_url = favorites_page.url.lower()
        if "logon" in current_url or "login" in current_url:
            print("[UYARI] Zara oturumu düştü, yeniden giriş deneniyor...")
            if not await ensure_zara_login(context, favorites_page):
                send_telegram_message("🚨 Zara oturumu düştü ve yenilenemedi.")
                return

        await scroll_page_to_bottom(favorites_page)

        candidates = await _extract_stock_and_discounted_items(favorites_page)
        print(
            f"[Zara] Hem stokta hem indirimde {len(candidates)} ürün bulundu. "
            "Tüm yan sekmeler eşzamanlı açılıyor..."
        )

        matched_items: list[dict] = []
        if candidates:
            results = await asyncio.gather(
                *[_open_and_check_tab(context, prod) for prod in candidates]
            )
            matched_items = [r for r in results if r is not None]

        notifier.process_cycle(matched_items)
        await save_store_session(context, "zara")

        remaining = max_seconds - (time.time() - start_time)
        if remaining <= REFRESH_INTERVAL:
            break

        print(f"[Zara] Tarama tamamlandı. {REFRESH_INTERVAL} sn bekleniyor...")
        await asyncio.sleep(REFRESH_INTERVAL)
