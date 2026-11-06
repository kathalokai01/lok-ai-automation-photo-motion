#!/usr/bin/env python3
"""Install and verify Hindi caption fonts with reusable local/system fonts."""

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
FONT_DIR = Path.home() / ".local" / "share" / "fonts" / "katha-caption"
MANIFEST = FONT_DIR / "font_install_manifest.json"

VERSION = "caption-font-installer-v6-correct-google-font-paths"
MAX_BYTES = 20 * 1024 * 1024
MIN_BYTES = 1024
SIGNATURES = {b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"}

FONT_FILES = {
    "NotoSansDevanagari-Regular.ttf": {
        "url": (
            "https://raw.githubusercontent.com/google/fonts/main/"
            "ofl/notosansdevanagari/"
            "NotoSansDevanagari%5Bwdth%2Cwght%5D.ttf"
        ),
        "family": "Noto Sans Devanagari",
        "required": True,
    },
    "Mukta-Regular.ttf": {
        "url": (
            "https://raw.githubusercontent.com/google/fonts/main/"
            "ofl/mukta/Mukta-Regular.ttf"
        ),
        "family": "Mukta",
        "required": False,
    },
    "Hind-Regular.ttf": {
        "url": (
            "https://raw.githubusercontent.com/google/fonts/main/"
            "ofl/hind/Hind-Regular.ttf"
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


def log(*items):
    print(*items, flush=True)


def run(command, timeout=45):
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_manifest():
    try:
        value = json.loads(MANIFEST.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def require_tools():
    missing = [
        name for name in ("fc-cache", "fc-match", "fc-scan")
        if shutil.which(name) is None
    ]
    if missing:
        raise RuntimeError(
            "Missing fontconfig tools: " + ", ".join(missing)
        )


def family_matches(actual, expected):
    names = [
        item.strip().casefold()
        for item in actual.split(",")
        if item.strip()
    ]
    target = expected.casefold()
    return any(
        name == target or name.startswith(target + " ")
        for name in names
    )


def validate_file(path):
    if not path.is_file():
        raise RuntimeError(f"Font file not found: {path}")

    size = path.stat().st_size
    if not MIN_BYTES <= size <= MAX_BYTES:
        raise RuntimeError(
            f"Unexpected font size: {path.name} ({size} bytes)"
        )

    with path.open("rb") as stream:
        signature = stream.read(4)

    if signature not in SIGNATURES:
        raise RuntimeError(
            f"Invalid font signature: {path.name}"
        )


def scan_family(path):
    result = run([
        "fc-scan", "--format", "%{family}\n", str(path)
    ])
    if result.returncode:
        raise RuntimeError(
            f"fc-scan failed for {path.name}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def verify_font_file(path, expected_family):
    validate_file(path)
    actual = scan_family(path)

    if not family_matches(actual, expected_family):
        raise RuntimeError(
            f"Font family mismatch: expected {expected_family!r}, "
            f"got {actual!r}"
        )

    return {
        "family": expected_family,
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "verification": "font-file-verified",
    }


def system_font(family):
    result = run([
        "fc-match", "-f", "%{family}\n%{file}\n", family
    ])
    if result.returncode:
        return None

    lines = result.stdout.strip().splitlines()
    if len(lines) < 2:
        return None

    actual_family = lines[0].strip()
    path = Path(lines[1].strip())

    if not family_matches(actual_family, family):
        return None
    if not path.is_file():
        return None

    try:
        record = verify_font_file(path, family)
    except (OSError, RuntimeError):
        return None

    record["verification"] = "existing-system-font-verified"
    return record


def download_font(filename, details):
    destination = FONT_DIR / filename
    temporary = destination.with_suffix(
        destination.suffix + ".download"
    )
    request = urllib.request.Request(
        details["url"],
        headers={
            "User-Agent": "KathaLokAI-CaptionFontInstaller/6.0",
            "Accept": "application/octet-stream",
        },
    )

    try:
        log("Downloading:", details["family"])
        with urllib.request.urlopen(request, timeout=90) as response:
            data = response.read(MAX_BYTES + 1)

        if not MIN_BYTES <= len(data) <= MAX_BYTES:
            raise RuntimeError(
                f"Invalid download size: {len(data)} bytes"
            )

        temporary.write_bytes(data)
        verify_font_file(temporary, details["family"])
        os.replace(temporary, destination)
        return verify_font_file(destination, details["family"])

    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        RuntimeError,
    ) as exc:
        raise RuntimeError(
            f"{filename}: {exc}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def resolve_installed_font(filename, details, old_manifest):
    local_path = FONT_DIR / filename
    old_fonts = old_manifest.get("fonts", {})
    old_record = (
        old_fonts.get(filename, {})
        if isinstance(old_fonts, dict)
        else {}
    )

    if local_path.is_file():
        try:
            record = verify_font_file(
                local_path, details["family"]
            )
            record["source"] = "project-font-cache"
            record["status"] = "verified"

            if old_record.get("sha256") == record["sha256"]:
                log("CHECKPOINT REUSED:", details["family"])
            else:
                log("VALID EXISTING FONT REUSED:", details["family"])
            return record
        except (OSError, RuntimeError) as exc:
            log("INVALID FONT CACHE:", filename, "|", exc)

    existing = system_font(details["family"])
    if existing:
        existing["source"] = "system-font"
        existing["status"] = "verified"
        log("SYSTEM FONT REUSED:", details["family"])
        return existing

    record = download_font(filename, details)
    record["source"] = "downloaded-free-font"
    record["status"] = "verified"
    log("FONT DOWNLOADED AND VERIFIED:", details["family"])
    return record

def main():
    require_tools()
    FONT_DIR.mkdir(parents=True, exist_ok=True)

    old_manifest = load_manifest()
    records = {}
    failures = {}

    for filename, details in FONT_FILES.items():
        try:
            records[filename] = resolve_installed_font(
                filename,
                details,
                old_manifest,
            )
        except Exception as exc:
            failures[filename] = str(exc)
            log(
                "REQUIRED FONT FAILURE:"
                if details["required"]
                else "OPTIONAL FONT FAILURE:",
                details["family],
                "|",
                exc,
            )

    cache = run(["fc-cache", "-f"], timeout=120)
    if cache.returncode:
        raise RuntimeError(
            "fc-cache failed: " + cache.stderr.strip()
        )

    selected = None
    fallback_order = (
        "Noto Sans Devanagari",
        "Mukta",
        "Hind",
        "Tiro Devanagari Hindi",
    )

    for family in fallback_order:
        selected = system_font(family)
        if selected:
            selected["source"] = "fontconfig-verified"
            if family != "Noto Sans Devanagari":
                selected["fallback_for"] = "Noto Sans Devanagari"
            log("HINDI FONT SELECTED:", family)
            break

    if not selected:
        raise RuntimeError(
            "No verified Devanagari font is available. "
            "Download and system font checks all failed. "
            + json.dumps(failures, ensure_ascii=False)
        )

    atomic_json(MANIFEST, {
        "status": "completed",
        "version": VERSION,
        "font_directory": str(FONT_DIR),
        "fonts": records,
        "selected_hindi_font": selected,
        "font_failures": failures,
        "external_api_calls": 0,
        "paid_services_used": False,
        "note": (
            "Fonts are accepted only after font-file and fontconfig "
            "verification. Existing valid project/system fonts are reused."
        ),
    })

    log("CAPTION FONT INSTALLATION COMPLETE")
    log("Verified project font records:", len(records))
    log("Selected Hindi font:", selected["family"])
    log("Manifest:", MANIFEST)
    log("External API calls: 0")
    log("Paid services used: false")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION FONT INSTALLATION FAILED:", str(exc))
        sys.exit(1)