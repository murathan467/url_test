import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_env_file(env_path: str) -> None:
    """Load variables from .env without overriding existing environment values."""
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass

    if not os.path.exists(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
                val = val[1:-1]
            if key:
                os.environ.setdefault(key, val)


_load_env_file(os.path.join(SCRIPT_DIR, ".env"))

# ─── Telegram Ayarları ───────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ─── Ortak Hesap Bilgileri ────────────────────────────────────
COMMON_EMAIL = os.getenv("COMMON_EMAIL", "").strip()
COMMON_PASSWORD = os.getenv("COMMON_PASSWORD", "").strip()

# ─── Mağaza URL'leri ──────────────────────────────────────────
ZARA_HOME_URL = "https://www.zara.com/tr/tr/"
ZARA_LOGIN_URL = "https://www.zara.com/tr/tr/logon"
ZARA_FAVORITES_URL = "https://www.zara.com/tr/tr/user/wishlist"

MANGO_HOME_URL = "https://shop.mango.com/tr/tr"
MANGO_LOGIN_URL = "https://shop.mango.com/tr/tr/login"
MANGO_FAVORITES_URL = "https://shop.mango.com/tr/tr/favorites"

OXXO_HOME_URL = "https://www.oxxo.com.tr/"
OXXO_LOGIN_URL = "https://www.oxxo.com.tr/tr/p/User/Login"
OXXO_FAVORITES_URL = "https://www.oxxo.com.tr/tr/p/User/FollowList"

# ─── Dosya Yolları ────────────────────────────────────────────
def get_storage_state_path(store_name: str) -> str:
    """Return the storage_state.json file path for the given store."""
    return os.path.join(SCRIPT_DIR, f"{store_name.lower()}_storage_state.json")

# ─── Tarama ve Döngü Ayarları ────────────────────────────────
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", "30"))
MAX_ALERTS_PER_WAVE = int(os.getenv("MAX_ALERTS_PER_WAVE", "3"))
ERROR_COOLDOWN = int(os.getenv("ERROR_COOLDOWN", "120"))
MAX_RUN_MINUTES = int(os.getenv("MAX_RUN_MINUTES", "345"))

# ─── Beden Filtresi ─────────────────────────────────────────
TARGET_SIZES = [
    s.strip().upper()
    for s in os.getenv("TARGET_SIZES", "S,XS,34,36").split(",")
    if s.strip()
]
