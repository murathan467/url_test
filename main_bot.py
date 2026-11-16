import argparse
import asyncio
from config import MAX_RUN_MINUTES


async def _run_store(store: str) -> None:
    """Launch store browser, run polling loop, and persist session on exit."""
    from patchright.async_api import async_playwright
    from core.browser import launch_store_browser
    from core.session import save_store_session
    from mango.mango_bot import run_mango_loop
    from oxxo.oxxo_bot import run_oxxo_loop
    from zara.zara_bot import run_zara_loop

    async with async_playwright() as p:
        browser, context = await launch_store_browser(p, store)
        try:
            if store == "zara":
                await run_zara_loop(context, MAX_RUN_MINUTES)
            elif store == "mango":
                await run_mango_loop(context, MAX_RUN_MINUTES)
            elif store == "oxxo":
                await run_oxxo_loop(context, MAX_RUN_MINUTES)
        finally:
            await save_store_session(context, store)
            await context.close()
            await browser.close()


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
    asyncio.run(_run_store(args.store))


if __name__ == "__main__":
    main()
