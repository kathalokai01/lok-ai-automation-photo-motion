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
PROJECT_MANIFEST = ROOT / "output" / "fonts" / "font_manifest.json"
PROJECT_FONT_FILE = ROOT / "output" / "fonts" / "selected_hindi_caption_font.ttf"
PROJECT_LICENSE_FILE = ROOT / "output" / "fonts" / "OFL-1.1.txt"

VERSION = "caption-font-installer-v8-portable-font-copy-license-and-manifest"
MAX_BYTES = 20 * 1024 * 1024
MIN_BYTES = 1024
SIGNATURES = {b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"}

FONT_COPYRIGHTS = {
    "Noto Sans Devanagari": "Copyright 2022 The Noto Project Authors (https://github.com/notofonts/devanagari)",
    "Mukta": "Copyright (c) 2014, Girish Dalvi, Ek Type. All rights reserved.",
    "Hind": "Copyright (c) 2014, Indian Type Foundry (info@indiantypefoundry.com).",
    "Tiro Devanagari Hindi": "Copyright 2020 The Indigo Project Authors (https://github.com/TiroTypeworks/Indigo)",
}

OFL_LICENSE_BODY = """SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
"""

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


def atomic_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
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
                details["family"],
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

    manifest_data = {
        "status": "completed",
        "version": VERSION,
        "font_directory": str(FONT_DIR),
        "project_manifest": PROJECT_MANIFEST.relative_to(ROOT).as_posix(),
        "fallback_order": list(fallback_order),
        "fonts": records,
        "selected_hindi_font": selected,
        "font_failures": failures,
        "external_api_calls": 0,
        "paid_services_used": False,
        "note": (
            "Font files are accepted only after file-signature, family, "
            "SHA-256 and fontconfig verification. The selected fallback "
            "family and its absolute file path are recorded."
        ),
    }
    atomic_json(MANIFEST, manifest_data)
    atomic_json(PROJECT_MANIFEST, manifest_data)

    if not PROJECT_MANIFEST.is_file() or PROJECT_MANIFEST.stat().st_size <= 0:
        raise RuntimeError("Project caption-font manifest was not written.")

    log("CAPTION FONT INSTALLATION COMPLETE")
    log("Verified project font records:", len(records))
    log("Selected Hindi font:", selected["family"])
    log("Selected font path:", selected["path"])
    log("Cache manifest:", MANIFEST)
    log("Project manifest:", PROJECT_MANIFEST)
    log("External API calls: 0")
    log("Paid services used: false")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("CAPTION FONT INSTALLATION FAILED:", str(exc))
        sys.exit(1)
