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


async def handle_cookies(page: Page) -> None:
    """Close Oxxo cookie banner if visible."""
    cookie_selectors = [
        "#onetrust-accept-btn-handler",
        ".cookie-accept",
        "button:has-text('TÜM ÇEREZLERİ KABUL ET')",
        "button:has-text('Tüm Çerezleri Kabul Et')",
        "a:has-text('TÜM ÇEREZLERİ KABUL ET')",
    ]
    matched_cookie = await click_first_visible(
        page, cookie_selectors, timeout_ms=1000, post_wait_ms=800
    )
    if matched_cookie:
        print(f"[Oxxo] Çerezler kabul edildi ({matched_cookie}).")


async def has_block_or_captcha(page: Page) -> bool:
    """Detect CAPTCHA frames or WAF blocks without clicking or bypassing them."""
    block_markers = tuple(
        _norm_fold(m) for m in ("access denied", "attention required", "verify you are human")
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
        captcha_count = await page.locator(
            "iframe[title*='reCAPTCHA'], iframe[src*='recaptcha'], "
            "iframe[src*='captcha'], iframe[src*='turnstile'], #sec-overlay"
        ).count()
        if captcha_count > 0:
            return True
    except Exception:
        pass
    return False


async def _inspect_oxxo_page(page: Page) -> dict:
    """Inspect Oxxo URL and login form visibility to detect silent session drops."""
    url = (page.url or "").lower()
    has_login_form = False
    try:
        has_login_form = await page.locator(
            "input#login_UserName, input#login_Password"
        ).first.is_visible(timeout=800)
    except Exception:
        pass
    return {
        "is_followlist_url": "followlist" in url,
        "has_login_url": any(tok in url for tok in ("login", "uyegirisi", "uye-girisi")),
        "has_login_form": has_login_form,
    }


async def ensure_oxxo_login(context: BrowserContext, page: Page) -> bool:
    """Verify Oxxo session on FollowList or attempt login when no CAPTCHA blocks it."""
    try:
        await safe_goto(page, OXXO_FAVORITES_URL, timeout_ms=30000, retries=2)
        await page.wait_for_timeout(3000)
        await handle_cookies(page)

        if await has_block_or_captcha(page):
            print(f"[HATA] Oxxo takip listesinde Captcha/WAF engeli algılandı -> {await page_diagnostics(page)}")
            return False

        state = await _inspect_oxxo_page(page)
        print(
            f"[Oxxo] İlk sayfa kontrolü -> {await page_diagnostics(page)} | "
            f"followlist={state['is_followlist_url']} login_url={state['has_login_url']} "
            f"login_form={state['has_login_form']}"
        )

        if (
            state["is_followlist_url"]
            and not state["has_login_url"]
            and not state["has_login_form"]
            and not await has_block_or_captcha(page)
        ):
            print("[Oxxo] ✅ Kayıtlı oturum aktif, doğrudan takip listesine girildi.")
            await save_store_session(context, "oxxo", mark_valid=True)
            return True
    except Exception as exc:
        print(f"[HATA] Oxxo başlangıç oturum kontrolü hatası: {exc}")
        return False

    clear_session_valid_marker("oxxo")

    if not COMMON_EMAIL or not COMMON_PASSWORD:
        print("[HATA] COMMON_EMAIL veya COMMON_PASSWORD eksik.")
        return False

    print("[Oxxo] Oturum kapalı, otomatik giriş başlatılıyor...")
    try:
        await safe_goto(page, OXXO_LOGIN_URL, timeout_ms=30000, retries=2)
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

        post_login = await _inspect_oxxo_page(page)
        if post_login["has_login_url"] or post_login["has_login_form"] or await has_block_or_captcha(page):
            print(f"[HATA] Oxxo girişi tamamlanamadı -> {await page_diagnostics(page)}")
            return False

        await safe_goto(page, OXXO_FAVORITES_URL, timeout_ms=30000, retries=2)
        await page.wait_for_timeout(3000)
        await handle_cookies(page)

        follow_state = await _inspect_oxxo_page(page)
        if (
            not follow_state["is_followlist_url"]
            or follow_state["has_login_url"]
            or follow_state["has_login_form"]
        ):
            print(f"[HATA] Oxxo FollowList sayfasına erişilemedi -> {await page_diagnostics(page)}")
            return False

        print("[Oxxo] ✅ Otomatik giriş başarılı!")
        await save_store_session(context, "oxxo", mark_valid=True)
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


async def _wait_for_oxxo_product_ready(tab: Page, timeout_ms: int = 6000) -> str:
    """Wait until Oxxo product title/sizes render or a block screen appears."""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if await has_block_or_captcha(tab):
            return "blocked"
        try:
            ready = await tab.evaluate(
                """
                () => document.querySelector('h1.ProductHeader, .SizeArea, .Prices') !== null
                """
            )
            if ready:
                await tab.wait_for_timeout(500)
                return "ready"
        except Exception:
            pass
        await tab.wait_for_timeout(300)
    return "timeout"


async def _open_and_check_tab(
    context: BrowserContext, product: dict, sem: asyncio.Semaphore
) -> tuple[str, dict | None]:
    """Open Oxxo product in a new tab (bounded by sem); require BOTH discount and TARGET_SIZES stock."""
    target_upper = [str(s).upper() for s in TARGET_SIZES]
    async with sem:
        tab: Page | None = None
        try:
            tab = await context.new_page()
            await safe_goto(tab, product["url"], timeout_ms=30000, retries=2)
            ready = await _wait_for_oxxo_product_ready(tab, timeout_ms=6000)
            if ready == "blocked":
                print(f"  [UYARI] Oxxo ürün sekmesinde geçici engel ({product.get('name', 'Ürün')}).")
                return "error", product

            info = await tab.evaluate(
                """
                (targetSizes) => {
                    const info = { sizes: [], oldPrice: "", newPrice: "", hasDiscount: false, name: "" };

                    try {
                        const titleEl = document.querySelector('h1.ProductHeader, h1');
                        if (titleEl) info.name = titleEl.innerText.trim();

                        const detailRoot = document.querySelector('.ProductDetail, .ProductInfo, #ProductDetail, main') || document;
                        const redPriceEl = detailRoot.querySelector('.Prices span.color-red, span.Price.color-red, span.color-red')
                            || document.querySelector('.Prices span.color-red, span.Price.color-red');
                        const normalPriceEl = detailRoot.querySelector('h1.ProductHeader ~ span.Price')
                            || detailRoot.querySelector('.Prices span.Price:not(.color-red)')
                            || detailRoot.querySelector('.Prices span.Price')
                            || detailRoot.querySelector('span.Price')
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
                            const allPriceSpans = detailRoot.querySelectorAll('span');
                            for (let j = 0; j < allPriceSpans.length; j++) {
                                const cls = allPriceSpans[j].className;
                                const txt = (allPriceSpans[j].innerText || "").trim();
                                if (
                                    typeof cls === "string" &&
                                    cls.includes('color-red') &&
                                    txt.length <= 25 &&
                                    /\\d[\\d.,]*\\s*TL/i.test(txt)
                                ) {
                                    info.hasDiscount = true;
                                    info.newPrice = txt;
                                    break;
                                }
                            }
                        }
                    } catch (e) {}

                    const buttons = document.querySelectorAll('.SizeArea a, .outer a');
                    buttons.forEach(btn => {
                        const outerText = btn.innerText.trim().toUpperCase();
                        const dataStock = btn.getAttribute("data-stock");
                        const isOutOfStock = btn.closest('.outer') !== null
                            || dataStock === "False"
                            || (dataStock !== "True" && !btn.closest('.SizeArea'));

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
                return "match", item
            return "ok", None
        except Exception as exc:
            print(f"  [HATA] Oxxo sekme tarama hatası ({product.get('name', 'Ürün')}): {exc}")
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


async def run_oxxo_loop(context: BrowserContext, max_run_minutes: int) -> bool:
    """Run Oxxo FollowList polling loop with bounded concurrent discount + size checks."""
    favorites_page = await context.new_page()
    try:
        if not await ensure_oxxo_login(context, favorites_page):
            send_telegram_message(
                "🚨 Oxxo oturumu açılamadı (reCAPTCHA/Login engeli). "
                "Lütfen yerelde 'python export_session.py --store oxxo' çalıştırıp OXXO_STORAGE_STATE secret'ını güncelleyin."
            )
            return False

        notifier = StockWaveNotifier("OXXO")
        start_time = time.time()
        max_seconds = max_run_minutes * 60 if max_run_minutes > 0 else float("inf")
        cycle = 0
        sem = asyncio.Semaphore(4)
        last_known_total = 0
        consecutive_empty_reads = 0
        consecutive_reload_failures = 0
        consecutive_loop_errors = 0
        outage_alert_sent = False

        while (time.time() - start_time) < max_seconds:
            cycle += 1
            cycle_start = time.time()
            print(f"\n[OXXO — Tur #{cycle}] Takip listesi yenileniyor...")

            try:
                if favorites_page.is_closed():
                    print("[UYARI] Oxxo takip listesi sekmesi kapanmış, yeniden açılıyor...")
                    favorites_page = await context.new_page()
                    await safe_goto(favorites_page, OXXO_FAVORITES_URL, timeout_ms=30000, retries=2)
                    await favorites_page.wait_for_timeout(2500)
                elif cycle > 1 and cycle % RECYCLE_TAB_EVERY_CYCLES == 0:
                    print("[Oxxo] Bellek temizliği için takip listesi sekmesi yenileniyor...")
                    try:
                        await favorites_page.close()
                    except Exception:
                        pass
                    favorites_page = await context.new_page()
                    if not await safe_reload_or_goto(
                        favorites_page, OXXO_FAVORITES_URL, timeout_ms=30000
                    ):
                        consecutive_reload_failures += 1
                        remaining = max_seconds - (time.time() - start_time)
                        if remaining <= REFRESH_INTERVAL:
                            break
                        await asyncio.sleep(REFRESH_INTERVAL)
                        continue
                    await favorites_page.wait_for_timeout(2500)
                elif cycle > 1:
                    ok = await safe_reload_or_goto(
                        favorites_page, OXXO_FAVORITES_URL, timeout_ms=30000
                    )
                    if not ok:
                        consecutive_reload_failures += 1
                        if consecutive_reload_failures % 3 == 0:
                            print("[UYARI] Oxxo sekmesi yenileniyor (ardışık yükleme hatası)...")
                            try:
                                await favorites_page.close()
                            except Exception:
                                pass
                            favorites_page = await context.new_page()
                        if consecutive_reload_failures >= OUTAGE_ALERT_THRESHOLD and not outage_alert_sent:
                            outage_alert_sent = True
                            send_telegram_message(
                                f"⚠️ Oxxo takip listesine arka arkaya {consecutive_reload_failures} kez "
                                "erişilemedi. Tekrar denenmeye devam ediliyor."
                            )
                        remaining = max_seconds - (time.time() - start_time)
                        if remaining <= REFRESH_INTERVAL:
                            break
                        await asyncio.sleep(REFRESH_INTERVAL)
                        continue
                    await favorites_page.wait_for_timeout(2500)

                if consecutive_reload_failures > 0:
                    if outage_alert_sent:
                        print("[Oxxo] ✅ Bağlantı erişimi tekrar sağlandı.")
                        outage_alert_sent = False
                    consecutive_reload_failures = 0

                await handle_cookies(favorites_page)

                if await has_block_or_captcha(favorites_page):
                    print(f"[UYARI] Oxxo geçici engeli algılandı, {ERROR_COOLDOWN} sn bekleniyor...")
                    remaining = max_seconds - (time.time() - start_time)
                    if remaining <= ERROR_COOLDOWN:
                        break
                    await asyncio.sleep(ERROR_COOLDOWN)
                    continue

                page_state = await _inspect_oxxo_page(favorites_page)
                if page_state["has_login_url"] or page_state["has_login_form"]:
                    print("[UYARI] Oxxo oturumu düştü, yeniden giriş deneniyor...")
                    clear_session_valid_marker("oxxo")
                    if not await ensure_oxxo_login(context, favorites_page):
                        send_telegram_message("🚨 Oxxo oturumu düştü ve yenilenemedi.")
                        return False
                    page_state = await _inspect_oxxo_page(favorites_page)

                if not page_state["is_followlist_url"]:
                    print("[UYARI] Oxxo takip listesi dışına çıkıldığı algılandı, listeye dönülüyor...")
                    try:
                        await safe_goto(favorites_page, OXXO_FAVORITES_URL, timeout_ms=30000, retries=2)
                        await favorites_page.wait_for_timeout(2500)
                        await handle_cookies(favorites_page)
                    except Exception as exc:
                        print(f"[HATA] Oxxo takip listesine dönülemedi: {exc}")
                        remaining = max_seconds - (time.time() - start_time)
                        if remaining <= REFRESH_INTERVAL:
                            break
                        await asyncio.sleep(REFRESH_INTERVAL)
                        continue
                    page_state = await _inspect_oxxo_page(favorites_page)
                    if page_state["has_login_url"] or page_state["has_login_form"]:
                        print("[UYARI] Oxxo oturumu düştü, yeniden giriş deneniyor...")
                        clear_session_valid_marker("oxxo")
                        if not await ensure_oxxo_login(context, favorites_page):
                            send_telegram_message("🚨 Oxxo oturumu düştü ve yenilenemedi.")
                            return False

                await scroll_page_to_bottom(favorites_page)

                candidates = await _extract_oxxo_candidates(favorites_page)
                if len(candidates) == 0 and last_known_total > 0:
                    print(
                        f"[UYARI] Oxxo 0 aday gördü (önceki tur: {last_known_total}); "
                        "liste bir kez tazeleniyor..."
                    )
                    try:
                        await safe_goto(favorites_page, OXXO_FAVORITES_URL, timeout_ms=30000, retries=2)
                        await favorites_page.wait_for_timeout(3000)
                        await handle_cookies(favorites_page)
                        await scroll_page_to_bottom(favorites_page)
                        candidates = await _extract_oxxo_candidates(favorites_page)
                    except Exception as exc:
                        print(f"[UYARI] Oxxo boş liste kurtarma denemesi başarısız: {exc}")

                print(
                    f"[Oxxo] {len(candidates)} aday ürün bulundu. "
                    "Stok+indirim kontrolü için yan sekmeler (en fazla 4 eşzamanlı) açılıyor..."
                )
                if cycle == 1 and len(candidates) == 0:
                    print(f"[Oxxo] İlk tur 0 ürün teşhisi -> {await page_diagnostics(favorites_page)}")

                if len(candidates) == 0:
                    post_empty = await _inspect_oxxo_page(favorites_page)
                    if post_empty["has_login_url"] or post_empty["has_login_form"]:
                        print("[UYARI] Oxxo oturumu sayfa içinde düştü, yeniden giriş deneniyor...")
                        clear_session_valid_marker("oxxo")
                        if not await ensure_oxxo_login(context, favorites_page):
                            send_telegram_message("🚨 Oxxo oturumu düştü ve yenilenemedi.")
                            return False
                        continue

                    consecutive_empty_reads += 1
                    if last_known_total > 0 and consecutive_empty_reads < 2:
                        print(
                            "[UYARI] Oxxo geçici 0 ürün okudu; yanlış 'Stok Tükendi' önlemek için "
                            f"bu tur atlandı -> {await page_diagnostics(favorites_page)}"
                        )
                        remaining = max_seconds - (time.time() - start_time)
                        if remaining <= REFRESH_INTERVAL:
                            break
                        await asyncio.sleep(REFRESH_INTERVAL)
                        continue
                else:
                    last_known_total = len(candidates)
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
                            print(f"  [HATA] Oxxo sekme istisnası: {res}")
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
                        "[UYARI] Oxxo tüm ürün sekmeleri hata verdi; yanlış 'Stok Tükendi' "
                        "önlemek için bildirim değerlendirmesi atlandı."
                    )
                else:
                    notifier.process_cycle(matched_items, uncertain_urls=errored_urls)

                if len(candidates) > 0 and (cycle == 1 or cycle % SESSION_SAVE_EVERY_CYCLES == 0):
                    await save_store_session(context, "oxxo", mark_valid=True)

                consecutive_loop_errors = 0
            except Exception as exc:
                consecutive_loop_errors += 1
                print(f"[HATA] Oxxo tur #{cycle} beklenmeyen hata ({consecutive_loop_errors}): {exc}")
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
                f"[Oxxo] Tarama tamamlandı ({cycle_elapsed:.1f} sn). "
                f"{sleep_for:.1f} sn bekleniyor..."
            )
            await asyncio.sleep(sleep_for)

        if consecutive_reload_failures >= OUTAGE_ALERT_THRESHOLD:
            print(
                f"[HATA] Oxxo koşusu kesintisiz bağlantı arızası "
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
