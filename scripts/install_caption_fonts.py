#!/usr/bin/env python3
"""Install, cache and verify free Hindi caption fonts."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FONT_DIR = (
    Path.home()
    / ".local"
    / "share"
    / "fonts"
    / "katha-caption"
)

MANIFEST = FONT_DIR / "font_install_manifest.json"

MAX_FONT_BYTES = 20 * 1024 * 1024
MIN_FONT_BYTES = 1024
VERSION = "caption-font-installer-v3"

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


def log(*args):
    print(*args, flush=True)


def run(command, timeout=30):
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def sha256_file(path):
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def validate_font_file(path):
    if not path.is_file():
        raise RuntimeError(f"Font file does not exist: {path}")

    size = path.stat().st_size

    if not MIN_FONT_BYTES <= size <= MAX_FONT_BYTES:
        raise RuntimeError(
            f"Unexpected font size for {path.name}: {size} bytes"
        )

    with path.open("rb") as handle:
        signature = handle.read(4)

    if signature not in VALID_FONT_SIGNATURES:
        raise RuntimeError(
            f"Invalid font signature in {path.name}; "
            "the download may be an error page."
        )


def scan_font(path):
    scanner = shutil.which("fc-scan")

    if not scanner:
        raise RuntimeError(
            "fc-scan is missing; install fontconfig first."
        )

    result = run(
        [
            scanner,
            "--format",
            "%{family}\n",
            str(path),
        ],
        timeout=20,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Cannot inspect {path.name}: {result.stderr.strip()}"
        )

    return result.stdout.strip()


def family_matches(actual, expected):
    actual_families = [
        item.strip().casefold()
        for item in actual.split(",")
        if item.strip()
    ]

    expected = expected.casefold()

    return any(
        family == expected
        or family.startswith(expected + " ")
        for family in actual_families
    )


def verify_font(filename, details):
    path = FONT_DIR / filename

    validate_font_file(path)

    actual_family = scan_font(path)

    if not family_matches(actual_family, details["family"]):
        raise RuntimeError(
            f"Wrong family in {filename}: expected "
            f"{details['family']!r}, found {actual_family!r}"
        )

    # Ask Pillow to load the font as an additional sanity check.
    try:
        from PIL import ImageFont

        font = ImageFont.truetype(str(path), size=28)

        if font.getbbox("Hindi 123") is None:
            raise RuntimeError(
                f"Pillow could not measure text using {filename}"
            )

    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required to verify caption fonts."
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"Pillow cannot load {filename}: {exc}"
        ) from exc

    log(
        "FONT VERIFIED:",
        details["family"],
        "|",
        filename,
        "|",
        path.stat().st_size,
        "bytes",
    )

    return {
        "filename": filename,
        "family": details["family"],
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "path": str(path),
    }


def download_font(filename, details):
    destination = FONT_DIR / filename
    temporary = destination.with_suffix(
        destination.suffix + ".download"
    )

    request = urllib.request.Request(
        details["url"],
        headers={
            "User-Agent": "KathaLokAI-CaptionFontInstaller/3.0",
        },
    )

    try:
        log("Downloading font:", details["family"])

        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(MAX_FONT_BYTES + 1)

        if len(data) > MAX_FONT_BYTES:
            raise RuntimeError(
                f"Font download exceeds size limit: {filename}"
            )

        if len(data) < MIN_FONT_BYTES:
            raise RuntimeError(
                f"Font download is unexpectedly small: {filename}"
            )

        temporary.write_bytes(data)
        validate_font_file(temporary)

        # Check the family before replacing an existing valid file.
        actual_family = scan_font(temporary)

        if not family_matches(actual_family, details["family"]):
            raise RuntimeError(
                f"Downloaded font family mismatch for {filename}: "
                f"{actual_family!r}"
            )

        temporary.replace(destination)

        log("FONT DOWNLOADED:", filename)

    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        RuntimeError,
    ) as exc:
        raise RuntimeError(f"{filename}: {exc}") from exc

    finally:
        temporary.unlink(missing_ok=True)

    return destination


def verify_fontconfig(details):
    matcher = shutil.which("fc-match")

    if not matcher:
        raise RuntimeError("fc-match is missing.")

    result = run(
        [
            matcher,
            "-f",
            "%{family}\n%{file}\n",
            details["family"],
        ],
        timeout=20,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"fontconfig failed for {details['family']}: "
            f"{result.stderr.strip()}"
        )

    lines = result.stdout.strip().splitlines()

    if len(lines) < 2:
        raise RuntimeError(
            f"fontconfig returned incomplete data for {details['family']}"
        )

    actual_family = lines[0].strip()
    actual_path = Path(lines[1].strip())

    if not family_matches(actual_family, details["family"]):
        raise RuntimeError(
            f"fontconfig substituted a different font for "
            f"{details['family']}: {actual_family}"
        )

    if not actual_path.is_file():
        raise RuntimeError(
            f"fontconfig returned a missing file: {actual_path}"
        )

    log("FONTCONFIG VERIFIED:", details["family"])


def load_old_manifest():
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_manifest(font_records):
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "status": "completed",
        "version": VERSION,
        "font_directory": str(FONT_DIR),
        "fonts": font_records,
        "external_api_calls": 0,
        "paid_services_used": False,
    }

    temporary = MANIFEST.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(MANIFEST)


def require_tools():
    for command in ("fc-cache", "fc-match", "fc-scan"):
        if not shutil.which(command):
            raise RuntimeError(
                f"Required command missing: {command}"
            )


def main():
    require_tools()
    FONT_DIR.mkdir(parents=True, exist_ok=True)

    old_manifest = load_old_manifest()
    old_records = old_manifest.get("fonts", {})

    verified_records = {}
    downloaded = []
    reused = []

    for filename, details in FONT_FILES.items():
        path = FONT_DIR / filename
        old_record = old_records.get(filename, {})
        can_reuse = False

        # Reuse only when the installed file still matches the previous
        # manifest and its font family remains valid.
        if path.is_file() and isinstance(old_record, dict):
            try:
                current_hash = sha256_file(path)

                if (
                    old_manifest.get("status") == "completed"
                    and old_record.get("sha256") == current_hash
                    and old_record.get("family") == details["family"]
                ):
                    record = verify_font(filename, details)
                    can_reuse = True
                    reused.append(filename)
                    verified_records[filename] = record
                    log("CACHE HIT:", filename)

            except (OSError, RuntimeError) as exc:
                log("CACHE INVALID:", filename, "|", exc)

        if can_reuse:
            continue

        # Even without a manifest, a valid existing font can be reused.
        if path.is_file():
            try:
                record = verify_font(filename, details)
                verified_records[filename] = record
                reused.append(filename)
                log("EXISTING VALID FONT REUSED:", filename)
                continue
            except (OSError, RuntimeError) as exc:
                log("EXISTING FONT NEEDS REPAIR:", filename, "|", exc)

        download_font(filename, details)
        record = verify_font(filename, details)
        verified_records[filename] = record
        downloaded.append(filename)

    result = run(
        ["fc-cache", "-f", str(FONT_DIR)],
        timeout=60,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"fc-cache failed: {result.stderr.strip()}"
        )

    for filename, details in FONT_FILES.items():
        verify_fontconfig(details)

    save_manifest(verified_records)

    log("======================================")
    log("CAPTION FONT INSTALLATION COMPLETE")
    log("FONT COUNT:", len(verified_records))
    log("DOWNLOADED:", len(downloaded))
    log("REUSED:", len(reused))
    log("FONT DIRECTORY:", FONT_DIR)
    log("MANIFEST:", MANIFEST)
    log("EXTERNAL API CALLS: 0")
    log("PAID SERVICES USED: false")
    log("======================================")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION FONT INSTALLATION FAILED:", str(exc))
        sys.exit(1)