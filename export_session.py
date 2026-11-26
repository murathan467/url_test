import argparse
import base64
import os
import re
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

GITHUB_API_VERSION = "2022-11-28"

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


def _resolve_github_repo(cwd: str = SCRIPT_DIR) -> tuple[str, str] | None:
    """Parse GitHub (owner, repo) from the local git 'origin' remote URL."""
    try:
        res = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return None
    if res.returncode != 0:
        return None
    url = (res.stdout or "").strip()
    match = re.search(
        r"github\.com[:/](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$",
        url,
    )
    if not match:
        return None
    return match.group("owner"), match.group("repo")


def _get_github_token_from_git(cwd: str = SCRIPT_DIR) -> str | None:
    """Read a GitHub token from env or 'git credential fill' without ever logging it."""
    for env_key in ("GH_TOKEN", "GITHUB_TOKEN"):
        env_val = os.getenv(env_key, "").strip()
        if env_val:
            return env_val
    try:
        res = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:
        return None
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        if line.startswith("password="):
            token = line.split("=", 1)[1].strip()
            if token:
                return token
    return None


def _encrypt_github_secret(public_key_b64: str, secret_value: str) -> str:
    """Encrypt secret_value with a GitHub Actions repo public key using PyNaCl SealedBox."""
    from nacl import encoding, public

    pub_key = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(pub_key)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("ascii")


def _upload_secret_via_github_api(
    secret_name: str, secret_value: str, cwd: str = SCRIPT_DIR
) -> tuple[bool, str]:
    """Upload a GitHub Actions secret via REST API using git credential fill + PyNaCl."""
    repo_info = _resolve_github_repo(cwd)
    if not repo_info:
        return False, "GitHub 'origin' deposu tespit edilemedi."
    owner, repo = repo_info

    token = _get_github_token_from_git(cwd)
    if not token:
        return False, "Yerel git kimlik bilgisinden GitHub token alınamadı."

    try:
        import requests
    except ImportError:
        return False, "'requests' paketi kurulu değil."

    try:
        import nacl  # noqa: F401
    except ImportError:
        return False, "'pynacl' paketi kurulu değil (kurmak için: pip install pynacl)."

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    base_url = f"https://api.github.com/repos/{owner}/{repo}/actions/secrets"
    try:
        key_resp = requests.get(f"{base_url}/public-key", headers=headers, timeout=20)
    except Exception as exc:
        return False, f"GitHub public-key isteği başarısız: {exc}"

    if key_resp.status_code != 200:
        return False, f"GitHub public-key alınamadı (HTTP {key_resp.status_code})."

    key_data = key_resp.json()
    key_id = key_data.get("key_id")
    pub_key = key_data.get("key")
    if not key_id or not pub_key:
        return False, "GitHub public-key yanıtı eksik."

    try:
        encrypted_value = _encrypt_github_secret(pub_key, secret_value)
    except Exception as exc:
        return False, f"Secret şifreleme hatası: {exc}"

    try:
        put_resp = requests.put(
            f"{base_url}/{secret_name}",
            headers=headers,
            json={"encrypted_value": encrypted_value, "key_id": key_id},
            timeout=20,
        )
    except Exception as exc:
        return False, f"GitHub Secret yükleme isteği başarısız: {exc}"

    if put_resp.status_code in (201, 204):
        return True, f"{owner}/{repo}"
    return False, f"GitHub Secret yükleme reddedildi (HTTP {put_resp.status_code})."


def _upload_github_secret(
    secret_name: str, secret_value: str, cwd: str = SCRIPT_DIR
) -> bool:
    """Upload a GitHub Actions secret via gh CLI when available, otherwise REST API."""
    if shutil.which("gh"):
        res = subprocess.run(
            ["gh", "secret", "set", secret_name],
            input=secret_value,
            text=True,
            cwd=cwd,
            check=False,
        )
        if res.returncode == 0:
            print(f"✅ GitHub Secret otomatik güncellendi (gh CLI): {secret_name}")
            return True
        print(
            f"⚠️ gh secret set başarısız oldu (kod: {res.returncode}); "
            "GitHub REST API ile yeniden deneniyor..."
        )

    ok, detail = _upload_secret_via_github_api(secret_name, secret_value, cwd=cwd)
    if ok:
        print(f"✅ GitHub Secret otomatik güncellendi (GitHub API, {detail}): {secret_name}")
        return True
    print(f"⚠️ GitHub Secret otomatik yüklenemedi ({detail}).")
    return False


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
                else:
                    _upload_github_secret(secret_name, encoded, cwd=SCRIPT_DIR)
            else:
                print(
                    f"\nGüvenlik için Base64 oturum metni terminale yazdırılmadı.\n"
                    f"Kaydedilen dosya: {b64_path}\n"
                    f"GitHub Secrets -> {secret_name} otomatik yüklemek için:\n"
                    f"  python export_session.py --store {store} --upload-secret"
                )
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
