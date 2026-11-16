import asyncio
import time
from patchright.async_api import BrowserContext, Page
from config import (
    COMMON_EMAIL,
    COMMON_PASSWORD,
    ERROR_COOLDOWN,
    OXXO_FAVORITES_URL,
    OXXO_LOGIN_URL,
    REFRESH_INTERVAL,
    TARGET_SIZES,
)
from core.browser import click_first_visible, human_type, scroll_page_to_bottom
from core.notifier import StockWaveNotifier, send_telegram_message
from core.session import save_store_session


async def handle_cookies(page: Page) -> None:
    """Close Oxxo cookie banner if visible."""
    cookie_selectors = [
        "#onetrust-accept-btn-handler",
        ".cookie-accept",
        "button:has-text('TÜM ÇEREZLERİ KABUL ET')",
        "button:has-text('Tüm Çerezleri Kabul Et')",
        "a:has-text('TÜM ÇEREZLERİ KABUL ET')",
    ]
    if await click_first_visible(page, cookie_selectors, timeout_ms=2000, post_wait_ms=1000):
        print("[Oxxo] Çerezler kabul edildi.")


async def has_block_or_captcha(page: Page) -> bool:
    """Detect CAPTCHA frames or WAF blocks without clicking or bypassing them."""
    try:
        text = (await page.locator("body").inner_text(timeout=2500)).lower()
        if "access denied" in text:
            return True
        captcha_count = await page.locator(
            "iframe[title*='reCAPTCHA'], iframe[src*='recaptcha'], iframe[src*='captcha'], #sec-overlay"
        ).count()
        if captcha_count > 0:
            return True
    except Exception:
        pass
    return False


async def ensure_oxxo_login(context: BrowserContext, page: Page) -> bool:
    """Verify Oxxo session on FollowList or attempt login when no CAPTCHA blocks it."""
    await page.goto(OXXO_FAVORITES_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3500)
    await handle_cookies(page)

    if "login" not in page.url.lower() and not await has_block_or_captcha(page):
        print("[Oxxo] ✅ Kayıtlı oturum aktif, doğrudan takip listesine girildi.")
        await save_store_session(context, "oxxo")
        return True

    if not COMMON_EMAIL or not COMMON_PASSWORD:
        print("[HATA] COMMON_EMAIL veya COMMON_PASSWORD eksik.")
        return False

    print("[Oxxo] Oturum kapalı, otomatik giriş başlatılıyor...")
    try:
        await page.goto(OXXO_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        await handle_cookies(page)

        if await has_block_or_captcha(page):
            print(
                "[HATA] Oxxo giriş sayfasında reCAPTCHA/WAF engeli var. "
                "Lütfen yerelde 'python export_session.py --store oxxo' ile oturumu yenileyip Secret'ı güncelleyin."
            )
            return False

        email_input = page.locator("input#login_UserName").first
        await email_input.wait_for(state="visible", timeout=15000)
        await human_type(email_input, COMMON_EMAIL)
        await page.wait_for_timeout(800)

        pass_input = page.locator("input#login_Password").first
        await pass_input.wait_for(state="visible", timeout=15000)
        await human_type(pass_input, COMMON_PASSWORD)
        await page.wait_for_timeout(800)

        if await has_block_or_captcha(page):
            print("[HATA] Oxxo giriş formunda reCAPTCHA algılandı; işlem durduruldu.")
            return False

        await page.locator("a#LoginButton, button#LoginButton").first.click()
        await page.wait_for_timeout(5000)

        if "login" in page.url.lower() or await has_block_or_captcha(page):
            print("[HATA] Oxxo girişi tamamlanamadı.")
            return False

        await page.goto(OXXO_FAVORITES_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3500)
        await handle_cookies(page)

        if "login" in page.url.lower():
            print("[HATA] Oxxo FollowList sayfasına erişilemedi.")
            return False

        print("[Oxxo] ✅ Otomatik giriş başarılı!")
        await save_store_session(context, "oxxo")
        return True
    except Exception as exc:
        print(f"[HATA] Oxxo login hatası: {exc}")
        return False


async def _extract_oxxo_candidates(page: Page) -> list[dict]:
    """Extract followed product URLs from Oxxo FollowList or product cards."""
    return await page.evaluate(
        """
        () => {
            const results = [];
            const seenUrls = new Set();

            const followItems = document.querySelectorAll('.FollowListItems');
            if (followItems.length > 0) {
                followItems.forEach(row => {
                    const linkEl = row.querySelector('.FollowItemImage a[href]');
                    if (linkEl) {
                        const href = linkEl.href;
                        if (href && !href.includes("javascript") && !seenUrls.has(href)) {
                            seenUrls.add(href);
                            const name = linkEl.getAttribute("title") || linkEl.getAttribute("alt") || "Oxxo Ürün";
                            results.push({
                                name: name,
                                url: href,
                                oldPrice: "",
                                newPrice: "",
                                hasDiscount: false
                            });
                        }
                    }
                });
            } else {
                const cards = document.querySelectorAll('.ProductItemWrapper');
                cards.forEach(card => {
                    const linkEl = card.querySelector('.ProductContent a.product-link, a.pr-name');
                    if (!linkEl) return;

                    const href = linkEl.href;
                    if (!href || seenUrls.has(href)) return;
                    seenUrls.add(href);

                    results.push({
                        name: linkEl.innerText.trim() || "Oxxo Ürün",
                        url: href,
                        oldPrice: "",
                        newPrice: "",
                        hasDiscount: false
                    });
                });
            }

            return results;
        }
        """
    )


async def _open_and_check_tab(context: BrowserContext, product: dict) -> dict | None:
    """Open Oxxo product in a new tab concurrently; require BOTH discount and TARGET_SIZES stock."""
    target_upper = [str(s).upper() for s in TARGET_SIZES]
    tab = await context.new_page()
    try:
        await tab.goto(product["url"], wait_until="domcontentloaded", timeout=30000)
        await tab.wait_for_timeout(2200)

        info = await tab.evaluate(
            """
            (targetSizes) => {
                const info = { sizes: [], oldPrice: "", newPrice: "", hasDiscount: false, name: "" };

                try {
                    const titleEl = document.querySelector('h1.ProductHeader, h1');
                    if (titleEl) info.name = titleEl.innerText.trim();

                    const redPriceEl = document.querySelector('.Prices span.color-red, span.Price.color-red, span.color-red');
                    const normalPriceEl = document.querySelector('h1.ProductHeader ~ span.Price')
                        || document.querySelector('.Prices span.Price:not(.color-red)')
                        || document.querySelector('.Prices span.Price')
                        || document.querySelector('span.Price');

                    if (redPriceEl && redPriceEl.innerText.includes('TL')) {
                        info.hasDiscount = true;
                        info.newPrice = redPriceEl.innerText.trim();
                        if (normalPriceEl && normalPriceEl !== redPriceEl && normalPriceEl.innerText.includes('TL')) {
                            info.oldPrice = normalPriceEl.innerText.trim();
                        }
                    } else if (normalPriceEl) {
                        info.newPrice = normalPriceEl.innerText.trim();
                        if (normalPriceEl.className && normalPriceEl.className.includes('color-red')) {
                            info.hasDiscount = true;
                        }
                    }

                    if (!info.hasDiscount) {
                        const allPriceSpans = document.querySelectorAll('span');
                        for (let j = 0; j < allPriceSpans.length; j++) {
                            const cls = allPriceSpans[j].className;
                            if (typeof cls === "string" && cls.includes('color-red') && allPriceSpans[j].innerText.includes('TL')) {
                                info.hasDiscount = true;
                                info.newPrice = allPriceSpans[j].innerText.trim();
                                break;
                            }
                        }
                    }
                } catch (e) {}

                const buttons = document.querySelectorAll('.SizeArea a, .outer a');
                buttons.forEach(btn => {
                    const outerText = btn.innerText.trim().toUpperCase();
                    const isOutOfStock = btn.closest('.outer') !== null
                        || (btn.getAttribute("data-stock") !== "True" && !btn.closest('.SizeArea'));

                    if (!isOutOfStock) {
                        const sizeName = outerText.split("\\n")[0].trim();
                        for (const t of targetSizes) {
                            if (sizeName === t || new RegExp("\\\\b" + t + "\\\\b", "i").test(sizeName)) {
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
        print(f"  [HATA] Oxxo sekme tarama hatası ({product.get('name', 'Ürün')}): {exc}")
        return None
    finally:
        try:
            if not tab.is_closed():
                await tab.close()
        except Exception:
            pass


async def run_oxxo_loop(context: BrowserContext, max_run_minutes: int) -> None:
    """Run Oxxo FollowList polling loop with simultaneous multi-tab discount + size checks."""
    favorites_page = await context.new_page()
    if not await ensure_oxxo_login(context, favorites_page):
        send_telegram_message(
            "🚨 Oxxo oturumu açılamadı (reCAPTCHA/Login engeli). "
            "Lütfen yerelde 'python export_session.py --store oxxo' çalıştırıp OXXO_STORAGE_STATE secret'ını güncelleyin."
        )
        return

    notifier = StockWaveNotifier("OXXO")
    start_time = time.time()
    max_seconds = max_run_minutes * 60 if max_run_minutes > 0 else float("inf")
    cycle = 0

    while (time.time() - start_time) < max_seconds:
        cycle += 1
        print(f"\n[OXXO — Tur #{cycle}] Takip listesi yenileniyor...")

        if cycle > 1:
            try:
                await favorites_page.reload(wait_until="domcontentloaded", timeout=30000)
                await favorites_page.wait_for_timeout(3500)
            except Exception as exc:
                print(f"[HATA] Oxxo sayfası yenilenemedi: {exc}")
                await asyncio.sleep(REFRESH_INTERVAL)
                continue

        await handle_cookies(favorites_page)

        if await has_block_or_captcha(favorites_page):
            print(f"[UYARI] Oxxo geçici engeli algılandı, {ERROR_COOLDOWN} sn bekleniyor...")
            await asyncio.sleep(ERROR_COOLDOWN)
            continue

        if "login" in favorites_page.url.lower():
            print("[UYARI] Oxxo oturumu düştü, yeniden giriş deneniyor...")
            if not await ensure_oxxo_login(context, favorites_page):
                send_telegram_message("🚨 Oxxo oturumu düştü ve yenilenemedi.")
                return

        await scroll_page_to_bottom(favorites_page)

        candidates = await _extract_oxxo_candidates(favorites_page)
        print(
            f"[Oxxo] {len(candidates)} aday ürün bulundu. "
            "Stok+indirim kontrolü için tüm yan sekmeler eşzamanlı açılıyor..."
        )

        matched_items: list[dict] = []
        if candidates:
            results = await asyncio.gather(
                *[_open_and_check_tab(context, prod) for prod in candidates]
            )
            matched_items = [r for r in results if r is not None]

        notifier.process_cycle(matched_items)
        await save_store_session(context, "oxxo")

        remaining = max_seconds - (time.time() - start_time)
        if remaining <= REFRESH_INTERVAL:
            break

        print(f"[Oxxo] Tarama tamamlandı. {REFRESH_INTERVAL} sn bekleniyor...")
        await asyncio.sleep(REFRESH_INTERVAL)
