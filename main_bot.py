import argparse
import asyncio
import sys
from config import MAX_RUN_MINUTES


async def _run_store(store: str) -> bool:
    """Launch store browser, run polling loop, and persist session only when verified."""
    from patchright.async_api import async_playwright
    from core.browser import launch_store_browser
    from core.session import (
        clear_session_valid_marker,
        has_valid_session_marker,
        save_store_session,
    )
    from mango.mango_bot import run_mango_loop
    from oxxo.oxxo_bot import run_oxxo_loop
    from zara.zara_bot import run_zara_loop

    clear_session_valid_marker(store)
    ok = False
    async with async_playwright() as p:
        browser, context = await launch_store_browser(p, store)
        try:
            if store == "zara":
                ok = await run_zara_loop(context, MAX_RUN_MINUTES)
            elif store == "mango":
                ok = await run_mango_loop(context, MAX_RUN_MINUTES)
            elif store == "oxxo":
                ok = await run_oxxo_loop(context, MAX_RUN_MINUTES)
        finally:
            if ok or has_valid_session_marker(store):
                await save_store_session(context, store, mark_valid=True)
            else:
                print(
                    f"[UYARI] [{store.upper()}] Oturum doğrulanamadığı için mevcut "
                    "storage_state kaydedilmedi (cache zehirlenmesi önlendi)."
                )
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
    return ok


def main() -> None:
    """CLI entrypoint for running a single store tracker."""
    parser = argparse.ArgumentParser(
        description="Zara, Mango ve Oxxo stok + indirim takip botu."
    )
    parser.add_argument(
        "--store",
        required=True,
        choices=["zara", "mango", "oxxo"],
        help="Çalıştırılacak mağaza adı.",
    )
    args = parser.parse_args()
    ok = asyncio.run(_run_store(args.store))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
