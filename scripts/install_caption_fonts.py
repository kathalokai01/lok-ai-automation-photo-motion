#!/usr/bin/env python3
"""Install and verify free Hindi caption fonts with resumable fallbacks."""

from __future__ import annotations

import hashlib
import json
import os
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
VERSION = "caption-font-installer-v4-resilient"

FONT_FILES = {
    "NotoSansDevanagari-Regular.ttf": {
        "url": (
            "https://raw.githubusercontent.com/google/fonts/main/"
            "ofl/notosansdevanagari/NotoSansDevanagari%5Bwght%5D.ttf"
        ),
        "family": "Noto Sans Devanagari",
        "required": True,
    },
    "Mukta-Regular.ttf": {
        "url": (
            "https://raw.githubusercontent.com/google/fonts/main/"
            "ofl/mukta/Mukta%5Bwght%5D.ttf"
        ),
        "family": "Mukta",
        "required": False,
    },
    "Hind-Regular.ttf": {
        "url": (
            "https://raw.githubusercontent.com/google/fonts/main/"
            "ofl/hind/Hind%5Bwght%5D.ttf"
        ),
        "family": "Hind",
        "required": False,
    },
    "TiroDevanagariHindi-Regular.ttf": {
        "url": (
            "https://raw.githubusercontent.com/google/fonts/main/"
            "ofl/tirodevanagarihindi/"
            "TiroDevanagariHindi-Regular.ttf"
        ),
        "family": "Tiro Devanagari Hindi",
        "required": False,
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


def atomic_write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
            "download may be an error page."
        )


def scan_font(path):
    scanner = shutil.which("fc-scan")
    if not scanner:
        raise RuntimeError("fc-scan is missing; install fontconfig.")

    result = run(
        [scanner, "--format", "%{family}\n", str(path)],
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Cannot inspect {path.name}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def family_matches(actual, expected):
    families = [
        item.strip().casefold()
        for item in actual.split(",")
        if item.strip()
    ]
    expected = expected.casefold()

    return any(
        family == expected or family.startswith(expected + " ")
        for family in families
    )


def verify_font(filename, details):
    path = FONT_DIR / filename
    validate_font_file(path)

    actual_family = scan_font(path)
    if not family_matches(actual_family, details["family"]):
        raise RuntimeError(
            f"Font family mismatch for {filename}: "
            f"expected {details['family']!r}, found {actual_family!r}"
        )

    try:
        from PIL import ImageFont
        font = ImageFont.truetype(str(path), size=28)
        if font.getbbox("Hindi 123") is None:
            raise RuntimeError(
                f"Pillow cannot measure text using {filename}"
            )
    except ImportError as exc:
        raise RuntimeError("Pillow is required to verify fonts.") from exc
    except OSError as exc:
        raise RuntimeError(
            f"Pillow cannot load {filename}: {exc}"
        ) from exc

    record = {
        "filename": filename,
        "family": details["family"],
        "required": details["required"],
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "path": str(path),
    }

    log("FONT VERIFIED:", details["family"], "|", filename)
    return record


def download_font(filename, details):
    destination = FONT_DIR / filename
    temporary = destination.with_suffix(destination.suffix + ".download")

    request = urllib.request.Request(
        details["url"],
        headers={"User-Agent": "KathaLokAI-CaptionFontInstaller/4.0"},
    )

    try:
        log("Downloading font:", details["family"])

        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(MAX_FONT_BYTES + 1)

        if len(data) > MAX_FONT_BYTES:
            raise RuntimeError("Download exceeds font size limit.")
        if len(data) < MIN_FONT_BYTES:
            raise RuntimeError("Download is unexpectedly small.")

        temporary.write_bytes(data)
        validate_font_file(temporary)

        actual_family = scan_font(temporary)
        if not family_matches(actual_family, details["family"]):
            raise RuntimeError(
                f"Downloaded font family mismatch: {actual_family!r}"
            )

        # Only replace the destination after validating the temporary file.
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
            matcher, "-f", "%{family}\n%{file}\n",
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
            f"fontconfig substituted {actual_family!r} for "
            f"{details['family']!r}"
        )

    if not actual_path.is_file():
        raise RuntimeError(
            f"fontconfig returned a missing file: {actual_path}"
        )

    return str(actual_path)


def load_old_manifest():
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def require_tools():
    missing = [
        name for name in ("fc-cache", "fc-match", "fc-scan")
        if shutil.which(name) is None
    ]
    if missing:
        raise RuntimeError(
            "Required font tools missing: " + ", ".join(missing)
        )


def install_one(filename, details, old_manifest):
    path = FONT_DIR / filename
    old_records = old_manifest.get("fonts", {})
    old_record = (
        old_records.get(filename, {})
        if isinstance(old_records, dict)
        else {}
    )

    if path.is_file():
        try:
            record = verify_font(filename, details)
            if (
                old_manifest.get("status") == "completed"
                and isinstance(old_record, dict)
                and old_record.get("sha256") == record["sha256"]
            ):
                log("CHECKPOINT REUSED:", filename)
            else:
                log("VALID EXISTING FONT REUSED:", filename)
            return record, "reused"
        except (OSError, RuntimeError) as exc:
            log("FONT CACHE INVALID:", filename, "|", exc)

    download_font(filename, details)
    return verify_font(filename, details), "downloaded"


def main():
    require_tools()
    FONT_DIR.mkdir(parents=True, exist_ok=True)

    old_manifest = load_old_manifest()
    records = {}
    failures = {}
    downloaded = []
    reused = []

    for filename, details in FONT_FILES.items():
        try:
            record, action = install_one(
                filename, details, old_manifest
            )
            records[filename] = record

            if action == "downloaded":
                downloaded.append(filename)
            else:
                reused.append(filename)

        except Exception as exc:
            failures[filename] = str(exc)
            log(
                "OPTIONAL FONT FAILURE:" if not details["required"]
                else "REQUIRED FONT FAILURE:",
                filename,
                "|",
                exc,
            )

            if details["required"]:
                raise RuntimeError(
                    "Required Hindi font could not be installed. "
                    "Fix the font download or network issue before "
                    "continuing. Details: " + str(exc)
                ) from exc

    if not records:
        raise RuntimeError("No usable caption fonts were installed.")

    result = run(["fc-cache", "-f", str(FONT_DIR)], timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"fc-cache failed: {result.stderr.strip()}")

    fontconfig_results = {}
    for filename, record in records.items():
        details = FONT_FILES[filename]
        try:
            fontconfig_results[filename] = {
                "status": "verified",
                "resolved_path": verify_fontconfig(details),
            }
        except Exception as exc:
            # Do not silently claim that fontconfig selected this font.
            fontconfig_results[filename] = {
                "status": "unverified",
                "error": str(exc),
            }
            log("FONTCONFIG WARNING:", details["family"], "|", exc)

            if details["required"]:
                raise RuntimeError(
                    "Required Hindi font is installed but fontconfig "
                    "cannot resolve it: " + str(exc)
                ) from exc

    manifest = {
        "status": "completed",
        "version": VERSION,
        "font_directory": str(FONT_DIR),
        "fonts": records,
        "fontconfig": fontconfig_results,
        "optional_font_failures": failures,
        "downloaded": downloaded,
        "reused": reused,
        "external_api_calls": 0,
        "paid_services_used": False,
    }
    atomic_write_json(MANIFEST, manifest)

    log("======================================")
    log("CAPTION FONT INSTALLATION COMPLETE")
    log("VERIFIED FONT FILES:", len(records))
    log("DOWNLOADED:", len(downloaded))
    log("REUSED:", len(reused))
    log("OPTIONAL FONT FAILURES:", len(failures))
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