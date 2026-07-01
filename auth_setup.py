"""
One-time Garmin token generator — RUN THIS LOCALLY, not on the server.

Why: Garmin login can trigger an MFA one-time code, which needs interactive input.
You do that once here. It prints a long token blob that you paste into Railway as the
GARMIN_TOKENS env var. The server then authenticates with no MFA. Tokens last ~1 year
and auto-refresh; re-run this if they ever expire or you change your password.

Usage:
    pip install garminconnect
    python auth_setup.py
    # enter email, password, and MFA code if prompted
    # copy the printed blob into Railway -> Variables -> GARMIN_TOKENS
"""

from __future__ import annotations

import getpass
import os
import sys

from garminconnect import Garmin


def main() -> int:
    email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = os.getenv("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")

    print("\nLogging in to Garmin Connect...", file=sys.stderr)
    client = Garmin(
        email,
        password,
        prompt_mfa=lambda: input("Enter Garmin MFA code (check email/SMS/app): ").strip(),
    )

    try:
        client.login()
    except Exception as exc:  # noqa: BLE001
        print(f"\nLogin failed: {exc}", file=sys.stderr)
        print(
            "If this is a TLS/Cloudflare or 401 error, retry from a different network, "
            "and make sure garminconnect is up to date (pip install -U garminconnect).",
            file=sys.stderr,
        )
        return 1

    name = None
    try:
        name = client.get_full_name()
    except Exception:  # noqa: BLE001
        pass

    blob = client.client.dumps()  # garminconnect 0.3.x: token serialization

    print(f"\n✅ Logged in{f' as {name}' if name else ''}.", file=sys.stderr)
    print(f"Token blob length: {len(blob)} chars.\n", file=sys.stderr)
    print("---------- COPY EVERYTHING BELOW INTO GARMIN_TOKENS ----------")
    print(blob)
    print("---------- END GARMIN_TOKENS ----------")
    print(
        "\nTip: in Railway, add a variable named GARMIN_TOKENS and paste the blob as its "
        "value (single line, no quotes).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
