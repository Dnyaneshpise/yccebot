"""Encode the saved session as base64 for the GitHub Actions secret.

    python scripts/encode_state.py

Prints the base64 string to copy into the ``AWS_STORAGE_STATE_B64`` repository
secret, and can verify the result decodes correctly before you paste it.

Security note: the printed value is a live credential. Never commit it, never
paste it into a chat or issue, and regenerate the session if it leaks.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = PROJECT_ROOT / "auth" / "storage_state.json"


def encode(path: Path) -> str:
    """Base64-encode a storage-state file as a single line."""
    raw = path.read_bytes()
    return base64.b64encode(raw).decode("ascii")


def verify(path: Path) -> bool:
    """Round-trip check that the file is a usable Playwright storage state."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        print(f"ERROR: could not read {path}: {type(exc).__name__}")
        return False

    if not isinstance(payload, dict) or "cookies" not in payload:
        print("ERROR: file is not a Playwright storage state (no 'cookies' key).")
        return False

    cookies = payload.get("cookies") or []
    print(f"OK: {len(cookies)} cookie(s), {len(payload.get('origins') or [])} origin(s)")

    try:
        base64.b64decode(encode(path), validate=True)
    except (binascii.Error, ValueError) as exc:
        print(f"ERROR: base64 round-trip failed: {type(exc).__name__}")
        return False

    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Storage state file to encode")
    args = parser.parse_args(argv)

    path = Path(args.input)
    if not path.exists():
        print(f"ERROR: {path} not found.")
        print("Run  python scripts/save_login_state.py  first.")
        return 1

    if not verify(path):
        return 1

    encoded = encode(path)
    line_count = encoded.count("\n")
    print(f"\nEncoded length: {len(encoded)} chars ({line_count} newlines - must be 0)")
    print("\n--- BEGIN AWS_STORAGE_STATE_B64 ---")
    print(encoded)
    print("--- END AWS_STORAGE_STATE_B64 ---\n")
    print("Add it as a GitHub Actions secret:")
    print("  gh secret set AWS_STORAGE_STATE_B64 < <(paste the value above)")
    print("or: Settings > Secrets and variables > Actions > New repository secret")
    return 0


if __name__ == "__main__":
    sys.exit(main())