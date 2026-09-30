"""Save an authenticated Playwright storage state for AWS Builder Center.

Run this ONCE locally (never in CI) to create ``auth/storage_state.json``:

    python scripts/save_login_state.py

A browser window opens, you sign in to builder.aws.com manually, then press
Enter in the terminal. The resulting session is stored locally and is gitignored.

Security note: this file grants access to your AWS Builder Center account.
Do not commit it, do not paste it into chat, and regenerate it if it leaks.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_OUTPUT = PROJECT_ROOT / "auth" / "storage_state.json"
DEFAULT_URL = "https://builder.aws.com/"


async def save_state(output: Path, url: str, timeout: int) -> int:
    """Open a headed browser, wait for manual login, then save the session."""
    from playwright.async_api import async_playwright

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        print(f"Reusing existing session file: {output}")
    else:
        print(f"Will save session to: {output}")

    print("\nA browser window will open.")
    print("1. Sign in to builder.aws.com in that window.")
    print("2. Wait until your dashboard is fully visible.")
    print("3. Come back here and press Enter.\n")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(locale="en-US")
        page = await context.new_page()

        try:
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            await asyncio.get_event_loop().run_in_executor(None, input, "Press Enter once you are signed in... ")
            await asyncio.sleep(3)

            cookies = await context.cookies()
            if not cookies:
                print("\nERROR: no cookies were captured - login did not complete.")
                return 1

            await context.storage_state(path=str(output))
        finally:
            await context.close()
            await browser.close()

    try:
        import os

        os.chmod(output, 0o600)  # owner-only: this is a credential
    except OSError:
        pass

    print(f"\nSaved {len(cookies)} cookies to {output}")
    print("Next step: run  python scripts/encode_state.py")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Where to save the session")
    parser.add_argument("--url", default=DEFAULT_URL, help="URL to open for login")
    args = parser.parse_args(argv)

    print("NOTE: run this locally on a machine with a display. CI cannot do this.")
    return asyncio.run(save_state(Path(args.output), args.url, timeout=0))


if __name__ == "__main__":
    sys.exit(main())