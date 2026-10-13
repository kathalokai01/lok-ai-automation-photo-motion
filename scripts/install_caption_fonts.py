#!/usr/bin/env python3
"""Install and verify free Devanagari caption fonts on Ubuntu runners."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

FONT_DIR = Path.home() / ".local" / "share" / "fonts" / "katha-caption"
FONT_DIR.mkdir(parents=True, exist_ok=True)

# Official Google Fonts repository raw files.
# A font is counted as installed only after downloading and validating
# a non-empty font file. If a source changes, the workflow fails clearly.
FONT_FILES = {
    "NotoSansDevanagari-Regular.ttf":
        "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansdevanagari/NotoSansDevanagari%5Bwght%5D.ttf",
    "Mukta-Regular.ttf":
        "https://raw.githubusercontent.com/google/fonts/main/ofl/mukta/Mukta%5Bwght%5D.ttf",
    "Hind-Regular.ttf":
        "https://raw.githubusercontent.com/google/fonts/main/ofl/hind/Hind%5Bwght%5D.ttf",
    "TiroDevanagariHindi-Regular.ttf":
        "https://raw.githubusercontent.com/google/fonts/main/ofl/tirodevanagarihindi/TiroDevanagariHindi-Regular.ttf",
}


def download_font(filename: str, url: str) -> Path:
    destination = FONT_DIR / filename
    temporary = destination.with_suffix(destination.suffix + ".tmp")

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "KathaLokAI-CaptionFontInstaller/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            content_type = response.headers.get("Content-Type", "")
            data = response.read(20 * 1024 * 1024 + 1)

        if len(data) < 1024 or len(data) > 20 * 1024 * 1024:
            raise RuntimeError(
                f"Unexpected download size for {filename}: {len(data)} bytes"
            )

        # True sfnt/OpenType files normally begin with one of these signatures.
        if data[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"):
            raise RuntimeError(
                f"Downloaded content is not a valid TTF/OTF file: "
                f"{filename} (Content-Type: {content_type})"
            )

        temporary.write_bytes(data)
        temporary.replace(destination)
        print(f"DOWNLOADED: {filename} ({len(data)} bytes)")
        return destination

    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    if not shutil.which("fc-cache") or not shutil.which("fc-match"):
        print(
            "ERROR: fontconfig tools are missing. "
            "Install fontconfig in the workflow first.",
            file=sys.stderr,
        )
        return 1

    failures = []

    for filename, url in FONT_FILES.items():
        try:
            download_font(filename, url)
        except Exception as exc:
            failures.append(f"{filename}: {exc}")
            print(f"FONT DOWNLOAD FAILED: {filename}: {exc}", file=sys.stderr)

    subprocess.run(
        ["fc-cache", "-f", str(FONT_DIR)],
        check=True,
        timeout=60,
    )

    font_names = [
        "Noto Sans Devanagari",
        "Mukta",
        "Hind",
        "Tiro Devanagari Hindi",
    ]

    missing = []
    for name in font_names:
        result = subprocess.run(
            ["fc-match", "-f", "%{family}\\n", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        matched = result.stdout.strip()
        print(f"FONT MATCH: {name} -> {matched or 'NO MATCH'}")

        if result.returncode != 0 or not matched:
            missing.append(name)

    if failures or missing:
        print("\nERROR: Not all requested fonts were verified.")
        for item in failures:
            print(f"- Download failure: {item}")
        for item in missing:
            print(f"- No fontconfig match: {item}")
        print("Do not start caption rendering with missing fonts.")
        return 1

    print("\nAll four caption font families passed fontconfig matching.")
    print(f"Font directory: {FONT_DIR}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"CAPTION FONT INSTALLATION FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)