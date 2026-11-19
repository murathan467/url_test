import os
from patchright.async_api import BrowserContext
from config import SCRIPT_DIR, get_storage_state_path

SESSION_CACHE_DIR = os.path.join(SCRIPT_DIR, ".session_cache")


def get_session_valid_marker_path(store_name: str) -> str:
    """Return the valid-session marker file path for the given store."""
    return os.path.join(SESSION_CACHE_DIR, f"{store_name.lower()}.valid")


def clear_session_valid_marker(store_name: str) -> None:
    """Remove the valid-session marker if present."""
    marker_path = get_session_valid_marker_path(store_name)
    try:
        if os.path.exists(marker_path):
            os.remove(marker_path)
    except OSError as exc:
        print(f"[UYARI] Oturum işaretçisi silinemedi ({store_name}): {exc}")


def mark_session_valid(store_name: str) -> None:
    """Write a valid-session marker after login/session verification succeeds."""
    marker_path = get_session_valid_marker_path(store_name)
    os.makedirs(SESSION_CACHE_DIR, exist_ok=True)
    with open(marker_path, "w", encoding="utf-8") as f:
        f.write("ok\n")


def has_valid_session_marker(store_name: str) -> bool:
    """Return True if the store has a valid-session marker file."""
    return os.path.isfile(get_session_valid_marker_path(store_name))


async def save_store_session(
    context: BrowserContext, store_name: str, mark_valid: bool = True
) -> bool:
    """Persist current browser cookies and localStorage to <store>_storage_state.json."""
    state_path = get_storage_state_path(store_name)
    try:
        await context.storage_state(path=state_path)
        if mark_valid:
            mark_session_valid(store_name)
        print(f"[{store_name.upper()}] Güncel oturum (cookies + localStorage) kaydedildi.")
        return True
    except Exception as exc:
        print(f"[HATA] Oturum kaydedilemedi ({store_name}): {exc}")
        return False
