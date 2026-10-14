#!/usr/bin/env python3
"""Package validated Katha Lok AI deliverables without nested ZIP files."""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
ARCHIVE = ROOT / "katha-lok-ai-photo-motion-output.zip"
REPORT = OUTPUT / "package_manifest.json"

sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes

VERSION = "output-packager-v1"
MIN_VIDEO_BYTES = 1000
MIN_THUMBNAIL_BYTES = 1000


def log(*args):
    print(*args, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_file(path, label, minimum_bytes=1):
    if not path.is_file():
        raise RuntimeError(f"{label} missing: {path.relative_to(ROOT)}")
    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(f"{label} too small: {path.relative_to(ROOT)}")
    return path


def read_json(path):
    require_file(path, "JSON manifest")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object: {path.name}")
    return data


def safe_output_file(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: path missing")

    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(OUTPUT.resolve()):
        raise RuntimeError(f"{label}: file must be inside output/")
    return require_file(path, label, MIN_VIDEO_BYTES)


def validate_deliverables(mode, parts, scenes):
    render_path = OUTPUT / "scene_renders" / "render_manifest.json"
    render = read_json(render_path)

    if render.get("status") != "completed":
        raise RuntimeError("Render manifest is not completed")
    if render.get("format") != mode:
        raise RuntimeError("Render manifest format mismatch")
    if render.get("captions_burned") is not True:
        raise RuntimeError("Final captions are not confirmed as burned")
    if int(render.get("completed_count", -1)) != parts * scenes:
        raise RuntimeError("Rendered scene count mismatch")

    rows = render.get("completed_scenes")
    if not isinstance(rows, list) or len(rows) != parts * scenes:
        raise RuntimeError("Rendered scene list is incomplete")

    for index, row in enumerate(rows, start=1):
        expected_part = (index - 1) // scenes + 1
        expected_scene = (index - 1) % scenes + 1

        part = int(row.get("part", 0))
        scene = int(row.get("local_scene", row.get("scene", 0)))
        global_scene = int(row.get("global_scene", index))

        if (part, scene, global_scene) != (
            expected_part, expected_scene, index
        ):
            raise RuntimeError(f"Scene order mismatch at scene {index}")

        if row.get("status") != "completed":
            raise RuntimeError(f"Scene not completed: {index}")
        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Scene captions missing: {index}")

        safe_output_file(row.get("video"), f"Captioned scene {index}")

    part_rows = render.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        raise RuntimeError(f"Expected exactly {parts} part videos")

    part_files = []
    for index, value in enumerate(part_rows, start=1):
        part_files.append(safe_output_file(value, f"Part video {index}"))

    final_file = safe_output_file(render.get("final_video"), "Final video")
    alias_file = safe_output_file(render.get("alias"), "Format alias")

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias_file.name != expected_alias:
        raise RuntimeError(f"Unexpected format alias: {alias_file.name}")

    opposite_alias = (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    if (OUTPUT / "videos" / opposite_alias).exists():
        raise RuntimeError(f"Unexpected opposite-format video: {opposite_alias}")

    thumbnail = require_file(
        OUTPUT / "thumbnail.jpg", "Thumbnail", MIN_THUMBNAIL_BYTES
    )
    metadata = require_file(
        OUTPUT / "youtube_metadata.txt", "YouTube metadata", 20
    )
    captions = require_file(
        OUTPUT / "captions" / "final_captions.srt", "Final captions SRT", 10
    )

    sound_manifest = read_json(
        OUTPUT / "sound_design" / "sound_design_manifest.json"
    )
    cue_manifest = read_json(
        OUTPUT / "sound_design" / "sfx_cues.json"
    )

    if sound_manifest.get("status") != "completed":
        raise RuntimeError("Sound-design generation is incomplete")
    if cue_manifest.get("status") != "completed":
        raise RuntimeError("SFX cue manifest is incomplete")

    return {
        "mode": mode,
        "parts": part_files,
        "final_video": final_file,
        "alias": alias_file,
        "thumbnail": thumbnail,
        "metadata": metadata,
        "captions": captions,
    }


def excluded_from_archive(path):
    relative = path.relative_to(OUTPUT)

    # Do not put archives, checkpoints, temporary files or cache sidecars
    # inside the downloadable output archive.
    if path.suffix.lower() == ".zip":
        return True
    if path.name.endswith((".tmp", ".tmp.mp4", ".concat.txt")):
        return True
    if path.name in {".DS_Store", "Thumbs.db"}:
        return True
    if "__pycache__" in relative.parts:
        return True
    if path.name.endswith(".caption.json"):
        return True

    return False


def build_file_list():
    files = []
    for path in OUTPUT.rglob("*"):
        if not path.is_file() or excluded_from_archive(path):
            continue
        files.append(path)

    return sorted(files, key=lambda item: item.relative_to(OUTPUT).as_posix())


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported format: {mode}")
    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")

    deliverables = validate_deliverables(mode, parts, scenes)
    files = build_file_list()

    if not files:
        raise RuntimeError("No output files to package")

    # Build outside output/ to prevent an archive from including itself.
    temporary = ARCHIVE.with_suffix(".zip.tmp")
    temporary.unlink(missing_ok=True)

    entries = []
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for path in files:
                name = path.relative_to(OUTPUT).as_posix()
                archive.write(path, arcname=f"output/{name}")
                entries.append({
                    "path": f"output/{name}",
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                })

        # Reopen the ZIP and verify its integrity before publishing it.
        with zipfile.ZipFile(temporary, "r") as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise RuntimeError(f"ZIP integrity failure: {bad_member}")

            names = archive.namelist()
            if len(names) != len(set(names)):
                raise RuntimeError("Duplicate ZIP entry paths detected")

            if any(name.lower().endswith(".zip") for name in names):
                raise RuntimeError("Nested ZIP detected in output archive")

        temporary.replace(ARCHIVE)

    finally:
        temporary.unlink(missing_ok=True)

    package_report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": parts,
        "scenes_per_part": scenes,
        "scene_count": parts * scenes,
        "archive": ARCHIVE.name,
        "archive_bytes": ARCHIVE.stat().st_size,
        "file_count": len(entries),
        "files": entries,
        "deliverables": {
            key: (
                [p.relative_to(ROOT).as_posix() for p in value]
                if isinstance(value, list)
                else value.relative_to(ROOT).as_posix()
            )
            for key, value in deliverables.items()
        },
    }

    # Report remains outside the archive because it describes the archive.
    REPORT.write_text(
        json.dumps(package_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    log("PACKAGING COMPLETE")
    log("FORMAT:", mode)
    log("PARTS:", parts)
    log("SCENES:", parts * scenes)
    log("FILES:", len(entries))
    log("ZIP:", ARCHIVE.name)
    log("ZIP BYTES:", ARCHIVE.stat().st_size)
    log("REPORT:", REPORT.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("PACKAGING FAILED:", str(exc))
        sys.exit(1)