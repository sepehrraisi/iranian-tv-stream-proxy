"""Import Sepehr OAuth constants from a local upstream source checkout.

This helper intentionally reads a file the user cloned and reviewed locally.
It never downloads remote code and never prints credential values.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


FIELDS = {
    "SEPEHR_OAUTH_CONSUMER_KEY": "oauth_consumer_key",
    "SEPEHR_OAUTH_SIGNATURE": "oauth_signature",
    "SEPEHR_OAUTH_TOKEN": "oauth_token",
}


def extract_oauth_values(source: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for env_name, header_name in FIELDS.items():
        match = re.search(
            rf'{header_name}=["\']([^"\']+)["\']',
            source,
            flags=re.IGNORECASE,
        )
        if not match:
            raise ValueError(f"Could not find {header_name} in the upstream source")
        values[env_name] = match.group(1)
    return values


def update_env(path: Path, values: dict[str, str]) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines()
    updated: list[str] = []
    remaining = dict(values)

    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in remaining:
            updated.append(f"{key}={remaining.pop(key)}")
        else:
            updated.append(line)

    if updated and updated[-1] != "":
        updated.append("")
    updated.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import OAuth constants from mobeigi/sepehr-stream-generator"
    )
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Path to sepehrstreamgenerator/browser.py in a local upstream clone",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Environment file to update (default: .env)",
    )
    args = parser.parse_args()

    if not args.source.is_file():
        parser.error(f"Upstream source file does not exist: {args.source}")

    try:
        values = extract_oauth_values(args.source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))

    update_env(args.env_file, values)
    print(f"Imported {len(values)} OAuth settings into {args.env_file}")
    print("Credential values were not displayed. Keep the environment file private.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
