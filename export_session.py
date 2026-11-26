import argparse
import base64
import os
import shutil
import subprocess
import time
from config import (
    MANGO_FAVORITES_URL,
    MANGO_LOGIN_URL,
    OXXO_FAVORITES_URL,
    OXXO_LOGIN_URL,
    SCRIPT_DIR,
    ZARA_FAVORITES_URL,
    ZARA_LOGIN_URL,
    get_storage_state_path,
)

STORE_URLS = {
    "zara": ZARA_LOGIN_URL,
    "mango": MANGO_LOGIN_URL,
    "oxxo": OXXO_LOGIN_URL,
}

STORE_FAVORITES = {
    "zara": ZARA_FAVORITES_URL,
    "mango": MANGO_FAVORITES_URL,
    "oxxo": OXXO_FAVORITES_URL,
}


def _verify_favorites_page(
    page, store: str, timeout_sec: float = 10.0, login_cta_grace_sec: float = 4.5
) -> tuple[bool, str]:
    """Wait for SPA hydration on favorites and verify that the session looks active.

    Does not abort on transient SSR login CTAs during initial hydration (e.g. Zara
    renders nav-to-login before /user/oauth2/refresh finishes); only fails once a
    login URL redirect occurs or login CTA/form persists past the grace window.
    """
    start = time.monotonic()
    deadline = start + timeout_sec
    last_summary = ""
    last_has_login_cta = False
    last_has_login_form = False
    while time.monotonic() < deadline:
        url = (page.url or "").lower()
        if any(tok in url for tok in ("logon", "login", "signin", "account.zara.com")):
            return False, f"Giriş sayfasına yönlendirildi ({page.url})"
        try:
            state = page.evaluate(
                """
                () => {
                    const norm = (s) => (s || "").replace(/[İIı]/g, "i").toLowerCase().replace(/\\u0307/g, "");
                    const mainEl = document.querySelector('main, [role="main"]') || document.body;
                    const txt = norm(mainEl ? mainEl.innerText : "");
                    const hasLoginForm = !!document.querySelector('input[type="password"]');
                    const hasNavLogin = !!document.querySelector(
                        '[data-qa-action="nav-to-login"], [data-qa-id="nav-to-login"]'
                    );
                    const prodCount = document.querySelectorAll(
                        'a[href*="-p"], a[href*="/p/"], .FollowListItems, .ProductItemWrapper'
                    ).length;
                    const hasLoginCta = prodCount === 0 && (
                        hasLoginForm ||
                        hasNavLogin ||
                        ["giriş yap", "oturum aç", "oturum açmalısınız", "log in", "sign in"]
                            .map(norm)
                            .some(k => txt.includes(k))
                    );
                    return { prodCount, hasLoginCta, hasLoginForm };
                }
                """
            )
            prod_count = int(state.get("prodCount", 0))
            last_has_login_cta = bool(state.get("hasLoginCta", False))
            last_has_login_form = bool(state.get("hasLoginForm", False))
            last_summary = f"url={page.url} urun_link={prod_count} login_cta={last_has_login_cta}"
            if prod_count > 0:
                page.wait_for_timeout(1000)
                return True, last_summary
            if last_has_login_form:
                return False, f"Sayfada giriş formu görünüyor ({last_summary})"
            if last_has_login_cta and (time.monotonic() - start) >= login_cta_grace_sec:
                return False, f"Sayfada giriş çağrısı görünüyor ({last_summary})"
        except Exception:
            pass
        page.wait_for_timeout(500)

    if last_has_login_cta or last_has_login_form:
        return False, f"Sayfada giriş çağrısı görünüyor ({last_summary})"
    return True, last_summary or f"url={page.url}"


def export_session(store: str, reuse_existing: bool = False, upload_secret: bool = False) -> None:
    """Open headed browser for manual login and export storage_state JSON + Base64."""
    from patchright.sync_api import sync_playwright
    from core.browser import _load_valid_storage_state

    login_url = STORE_URLS[store]
    state_path = get_storage_state_path(store)
    b64_path = os.path.join(SCRIPT_DIR, f"{store}_storage_state.b64")
    secret_name = f"{store.upper()}_STORAGE_STATE"

    print("=" * 60)
    print(f"  {store.upper()} OTURUM DIŞA AKTARMA ARACI")
    print("=" * 60)
    print(f"Açılan tarayıcıda ({login_url}) manuel olarak giriş yapın.")
    print("Giriş tamamlanıp favoriler/hesap açıldığında bu terminale dönüp ENTER'a basın.")

    with sync_playwright() as p:
        launch_kwargs: dict = {
            "headless": False,
            "args": ["--window-size=1920,1080", "--start-maximized", "--no-sandbox"],
        }
        if os.name == "nt":
            launch_kwargs["channel"] = "chrome"

        browser = p.chromium.launch(**launch_kwargs)
        context_kwargs: dict = {
            "viewport": {"width": 1920, "height": 1080},
            "locale": "tr-TR",
            "timezone_id": "Europe/Istanbul",
        }
        if reuse_existing:
            valid_state = _load_valid_storage_state(state_path)
            if valid_state:
                context_kwargs["storage_state"] = valid_state
                print(f"[BİLGİ] Mevcut oturum dosyası tekrar yüklendi: {valid_state}")

        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        verified = True
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            input("\n[BEKLENİYOR] Giriş tamamlandıysa ENTER tuşuna basın...")
            fav_url = STORE_FAVORITES.get(store)
            if fav_url:
                print(f"\nFavoriler sayfasına gidilerek oturum ve çerezler doğrulanıyor: {fav_url}")
                try:
                    page.goto(fav_url, wait_until="domcontentloaded", timeout=30000)
                    verified, detail = _verify_favorites_page(page, store)
                    if verified:
                        print(f"✅ Favoriler oturumu doğrulandı ({detail}).")
                    else:
                        print(
                            f"⚠️ UYARI: Oturum doğrulanamadı! ({detail})\n"
                            "   Giriş tamamlanmadıysa tarayıcıyı kapatıp tekrar deneyin."
                        )
                except Exception as e:
                    verified = False
                    print(f"Uyarı: Favoriler açılırken hata: {e}")

            context.storage_state(path=state_path)

            with open(state_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("ascii")
            with open(b64_path, "w", encoding="ascii") as f:
                f.write(encoded)

            print(f"\n✅ Oturum kaydedildi: {state_path}")
            print(f"✅ Base64 çıktı kaydedildi: {b64_path}")

            if upload_secret:
                if not verified:
                    print(
                        f"⚠️ Oturum doğrulanamadığı için {secret_name} otomatik yüklenmedi "
                        "(mevcut çalışan Secret korunuyor)."
                    )
                elif shutil.which("gh"):
                    res = subprocess.run(
                        ["gh", "secret", "set", secret_name],
                        input=encoded,
                        text=True,
                        cwd=SCRIPT_DIR,
                        check=False,
                    )
                    if res.returncode == 0:
                        print(f"✅ GitHub Secret otomatik güncellendi: {secret_name}")
                    else:
                        print(f"⚠️ gh secret set başarısız oldu (kod: {res.returncode}).")
                else:
                    print("⚠️ 'gh' CLI bulunamadı; GitHub Secret'ı manuel güncelleyin.")
            else:
                print(
                    f"\nGitHub Secrets -> {secret_name} alanına aşağıdaki Base64 metni "
                    f"yapıştırabilir veya şu komutu çalıştırabilirsiniz:\n"
                    f"  gh secret set {secret_name} < {os.path.basename(b64_path)}"
                )
                print("-" * 60)
                print(encoded)
                print("-" * 60)
        finally:
            context.close()
            browser.close()


def main() -> None:
    """CLI entrypoint for exporting local Playwright storage_state."""
    parser = argparse.ArgumentParser(
        description="Mağaza oturumunu manuel girişle storage_state.json olarak dışa aktarır."
    )
    parser.add_argument(
        "--store",
        required=True,
        choices=["zara", "mango", "oxxo"],
        help="Oturumu dışa aktarılacak mağaza adı.",
    )
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="Mevcut *_storage_state.json dosyasını başlangıçta yükle (varsayılan: temiz oturum).",
    )
    parser.add_argument(
        "--upload-secret",
        "--set-gh-secret",
        dest="upload_secret",
        action="store_true",
        help="Dışa aktarılan Base64 oturumu 'gh secret set' ile doğrudan GitHub Secrets'a yükle.",
    )
    args = parser.parse_args()
    export_session(args.store, reuse_existing=args.reuse, upload_secret=args.upload_secret)


if __name__ == "__main__":
    main()
