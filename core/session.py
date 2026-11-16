from patchright.async_api import BrowserContext
from config import get_storage_state_path


async def save_store_session(context: BrowserContext, store_name: str) -> None:
    """Persist current browser cookies and localStorage to <store>_storage_state.json."""
    state_path = get_storage_state_path(store_name)
    try:
        await context.storage_state(path=state_path)
        print(f"[{store_name.upper()}] Güncel oturum (cookies + localStorage) kaydedildi.")
    except Exception as exc:
        print(f"[HATA] Oturum kaydedilemedi ({store_name}): {exc}")
