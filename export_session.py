import argparse
import base64
import os
from config import (
    MANGO_LOGIN_URL,
    OXXO_LOGIN_URL,
    SCRIPT_DIR,
    ZARA_LOGIN_URL,
    get_storage_state_path,
)

STORE_URLS = {
    "zara": ZARA_LOGIN_URL,
    "mango": MANGO_LOGIN_URL,
    "oxxo": OXXO_LOGIN_URL,
}


def export_session(store: str) -> None:
    """Open headed browser for manual login and export storage_state JSON + Base64."""
    from patchright.sync_api import sync_playwright

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
        browser = p.chromium.launch(
            headless=False,
            args=["--window-size=1920,1080", "--start-maximized", "--no-sandbox"],
        )
        context_kwargs: dict = {
            "viewport": {"width": 1920, "height": 1080},
            "locale": "tr-TR",
            "timezone_id": "Europe/Istanbul",
        }
        if os.path.exists(state_path) and os.path.getsize(state_path) > 0:
            context_kwargs["storage_state"] = state_path

        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            input("\n[BEKLENİYOR] Giriş tamamlandıysa ENTER tuşuna basın...")
            context.storage_state(path=state_path)

            with open(state_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("ascii")
            with open(b64_path, "w", encoding="ascii") as f:
                f.write(encoded)

            print(f"\n✅ Oturum kaydedildi: {state_path}")
            print(f"✅ Base64 çıktı kaydedildi: {b64_path}")
            print(f"\nGitHub Secrets -> {secret_name} alanına aşağıdaki Base64 metni yapıştırabilirsiniz:")
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
    args = parser.parse_args()
    export_session(args.store)


if __name__ == "__main__":
    main()
