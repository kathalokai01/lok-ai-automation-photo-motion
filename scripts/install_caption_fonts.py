#!/usr/bin/env python3
"""Install and verify the four free Hindi caption font families."""

from __future__ import annotations

import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

FONT_DIR = (
    Path.home()
    / ".local"
    / "share"
    / "fonts"
    / "katha-caption"
)

MAX_FONT_BYTES = 20 * 1024 * 1024
MIN_FONT_BYTES = 1024

FONT_FILES = {
    "NotoSansDevanagari-Regular.ttf": {
        "url": (
            "https://raw.githubusercontent.com/google/fonts/main/"
            "ofl/notosansdevanagari/NotoSansDevanagari%5Bwght%5D.ttf"
        ),
        "family": "Noto Sans Devanagari",
    },
    "Mukta-Regular.ttf": {
        "url": (
            "https://raw.githubusercontent.com/google/fonts/main/"
            "ofl/mukta/Mukta%5Bwght%5D.ttf"
        ),
        "family": "Mukta",
    },
    "Hind-Regular.ttf": {
        "url": (
            "https://raw.githubusercontent.com/google/fonts/main/"
            "ofl/hind/Hind%5Bwght%5D.ttf"
        ),
        "family": "Hind",
    },
    "TiroDevanagariHindi-Regular.ttf": {
        "url": (
            "https://raw.githubusercontent.com/google/fonts/main/"
            "ofl/tirodevanagarihindi/"
            "TiroDevanagariHindi-Regular.ttf"
        ),
        "family": "Tiro Devanagari Hindi",
    },
}

VALID_FONT_SIGNATURES = {
    b"\x00\x01\x00\x00",
    b"OTTO",
    b"true",
    b"typ1",
}


def run(command: list[str], timeout: int = 30):
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def validate_font_file(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"Font file does not exist: {path}")

    size = path.stat().st_size

    if not MIN_FONT_BYTES <= size <= MAX_FONT_BYTES:
        raise RuntimeError(
            f"Unexpected font file size for {path.name}: {size} bytes"
        )

    with path.open("rb") as handle:
        signature = handle.read(4)

    if signature not in VALID_FONT_SIGNATURES:
        raise RuntimeError(
            f"Invalid font signature in {path.name}; "
            "the URL may have returned an error page instead of a font."
        )


def download_font(filename: str, details: dict) -> Path:
    destination = FONT_DIR / filename
    temporary = destination.with_suffix(destination.suffix + ".tmp")

    request = urllib.request.Request(
        details["url"],
        headers={
            "User-Agent": "KathaLokAI-CaptionFontInstaller/2.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(MAX_FONT_BYTES + 1)

        if len(data) > MAX_FONT_BYTES:
            raise RuntimeError(f"Font download is too large: {filename}")

        temporary.write_bytes(data)
        validate_font_file(temporary)
        temporary.replace(destination)

        print(
            f"FONT DOWNLOADED: {filename} "
            f"({destination.stat().st_size} bytes)"
        )
        return destination

    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        RuntimeError,
    ) as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"{filename}: {exc}") from exc


def scan_font(path: Path) -> str:
    """Read the actual family metadata stored in this font file."""
    if not shutil_which("fc-scan"):
        raise RuntimeError(
            "fc-scan is unavailable; install fontconfig in the workflow."
        )

    result = run(
        [
            "fc-scan",
            "--format",
            "%{family}\n",
            str(path),
        ],
        timeout=20,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Could not inspect font metadata for {path.name}: "
            f"{result.stderr.strip()}"
        )

    return result.stdout.strip()


def shutil_which(name: str) -> str | None:
    # Keep this module's imports minimal.
    import shutil
    return shutil.which(name)


def verify_font(filename: str, details: dict) -> None:
    path = FONT_DIR / filename
    validate_font_file(path)

    actual_family = scan_font(path)
    expected_family = details["family"].casefold()

    actual_families = [
        family.strip().casefold()
        for family in actual_family.split(",")
        if family.strip()
    ]

    if not any(
        family == expected_family
        or family.startswith(expected_family + " ")
        for family in actual_families
    ):
        raise RuntimeError(
            f"Wrong font family in {filename}. "
            f"Expected {details['family']!r}; "
            f"found {actual_family!r}."
        )

    print(f"FONT FILE VERIFIED: {details['family']} -> {actual_family}")


def verify_fontconfig(details: dict) -> None:
    family = details["family"]

    result = run(
        [
            "fc-match",
            "-f",
            "%{family}\n%{file}\n",
            family,
        ],
        timeout=20,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"fontconfig lookup failed for {family}: "
            f"{result.stderr.strip()}"
        )

    lines = result.stdout.strip().splitlines()

    if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
        raise RuntimeError(
            f"fontconfig could not resolve a font for {family}."
        )

    matched_families = [
        item.strip().casefold()
        for item in lines[0].split(",")
        if item.strip()
    ]

    expected = family.casefold()

    if not any(
        item == expected or item.startswith(expected + " ")
        for item in matched_families
    ):
        raise RuntimeError(
            f"fontconfig substituted another family for {family}: "
            f"{lines[0]}"
        )

    matched_path = Path(lines[1])

    if not matched_path.is_file():
        raise RuntimeError(
            f"fontconfig returned a missing font file for {family}: "
            f"{matched_path}"
        )

    print(f"FONTCONFIG VERIFIED: {family} -> {matched_path}")


def main() -> int:
    import shutil

    FONT_DIR.mkdir(parents=True, exist_ok=True)

    for command in ("fc-cache", "fc-match", "fc-scan"):
        if not shutil.which(command):
            print(
                f"ERROR: Required font tool is missing: {command}",
                file=sys.stderr,
            )
            return 1

    failures = []

    for filename, details in FONT_FILES.items():
        try:
            download_font(filename, details)
        except Exception as exc:
            failures.append(str(exc))
            print(f"DOWNLOAD ERROR: {exc}", file=sys.stderr)

    if failures:
        print(
            "ERROR: Font downloads failed. Caption rendering must not "
            "continue with an incomplete font set.",
            file=sys.stderr,
        )
        return 1

    try:
        result = run(
            ["fc-cache", "-f", str(FONT_DIR)],
            timeout=60,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"fc-cache failed: {result.stderr.strip()}"
            )

        for filename, details in FONT_FILES.items():
            verify_font(filename, details)
            verify_fontconfig(details)

    except Exception as exc:
        print(f"FONT VERIFICATION FAILED: {exc}", file=sys.stderr)
        return 1

    print("SUCCESS: All four Hindi caption font files were verified.")
    print(f"Font directory: {FONT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())