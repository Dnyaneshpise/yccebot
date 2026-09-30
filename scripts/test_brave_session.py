"""Test connecting to your already-signed-in Brave and reading badge state.

Run this to check whether a real browser session can do things the
Playwright-launched one cannot.

    python scripts/test_brave_session.py

Requires Brave to be running with a debug port (see the script output).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.builder import BuilderCenter  # noqa: E402

PORT = 9222


async def main() -> int:
    b = BuilderCenter(headless=False, cdp_endpoint=f"127.0.0.1:{PORT}")
    if not await b.connect_over_cdp():
        print("\nCould not attach. See the README section 'Attach to your browser'.")
        return 1

    try:
        if not await b.goto("https://builder.aws.com/"):
            print("Could not load Builder Center")
            return 1

        await b._dismiss_consent()
        await b.page.wait_for_timeout(2000)

        signed_in = await b.page.evaluate(
            """() => ({
                signOut: [...document.querySelectorAll('a,button')]
                    .filter(e => /sign out|log out/i.test(e.innerText || '')).length,
                signIn: [...document.querySelectorAll('a,button')]
                    .filter(e => /^(sign in|log in)$/i.test((e.innerText || '').trim())).length,
            })"""
        )
        print(f"\nSign-out controls: {signed_in['signOut']}  Sign-in controls: {signed_in['signIn']}")
        if signed_in["signOut"] == 0:
            print("-> This browser is NOT signed in to Builder Center.")
            print("   Sign in manually, then run this again.")
            return 1
        print("-> Signed in. Good.")

        detail = await b.get_badge_progress()
        if detail:
            print(f"\nBADGES: {detail['granted']} / {detail['total']}")
            for row in detail.get("badges", [])[:8]:
                print(f"   [{row['status']:<12}] {row['name']}")
        else:
            print("\nCould not read badge progress from this browser either.")

        print("\nNot attempting any write. Use the agent's approve flow for that.")
        return 0
    finally:
        await b.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))