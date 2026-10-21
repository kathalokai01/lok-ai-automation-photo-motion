
#!/usr/bin/env python3
"""Validate and package only the selected-format Katha Lok AI deliverables."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
ARCHIVE = ROOT / "katha-lok-ai-photo-motion-output.zip"
REPORT = OUTPUT / "package_manifest.json"
VALIDATION_REPORT = OUTPUT / "validation_report.json"
RENDER_MANIFEST = OUTPUT / "scene_renders" / "render_manifest.json"

VERSION = "output-packager-v9-curated-format-safe-zip"
VIDEO_SIZE = (1280, 720)
THUMBNAIL_SIZE = (1280, 720)

sys.path.insert(0, str(ROOT / "scripts"))
from input_config import load_and_validate, get_format, get_parts, get_scenes


def log(*values):
    print(*values, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    if not path.is_file():
        raise RuntimeError(f"Required JSON missing: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Invalid JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object: {path}")
    return data


def safe_path(value, label, minimum=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: missing path")
    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_relative_to(OUTPUT.resolve()):
        raise RuntimeError(f"{label}: path escapes output/: {path}")
    if not path.is_file() or path.stat().st_size < minimum:
        raise RuntimeError(f"{label}: missing or too small: {path}")
    return path


def relative(path):
    path = Path(path).resolve()
    if not path.is_relative_to(OUTPUT.resolve()):
        raise RuntimeError(f"Path outside output/: {path}")
    return path.relative_to(OUTPUT.resolve()).as_posix()


def archive_name(path):
    return "output/" + relative(path)


def media_info(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required")
    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=90, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"ffprobe failed for {path.name}: {result.stderr[-500:]}")
    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Invalid media information: {path.name}") from exc
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if duration <= 0:
        raise RuntimeError(f"Invalid duration: {path.name}")
    return {
        "duration": duration,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
    }


def require_av(path, label):
    info = media_info(path)
    if not info["has_video"] or not info["has_audio"]:
        raise RuntimeError(f"{label}: video and audio streams are both required")
    if (info["width"], info["height"]) != VIDEO_SIZE:
        raise RuntimeError(
            f"{label}: expected {VIDEO_SIZE[0]}x{VIDEO_SIZE[1]}, "
            f"got {info['width']}x{info['height']}"
        )
    return info


def validate_render(mode, parts, scenes):
    data = read_json(RENDER_MANIFEST)
    if data.get("status") != "completed":
        raise RuntimeError("Render manifest is not completed")
    if str(data.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Render manifest format does not match Input/topic.txt")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Manifest does not confirm burned-in captions")

    expected = parts * scenes
    rows = data.get("completed_scenes")
    if not isinstance(rows, list) or len(rows) != expected:
        raise RuntimeError(f"Expected {expected} scene rows in render manifest")
    if int(data.get("completed_count", -1)) != expected:
        raise RuntimeError("Render manifest completed_count mismatch")

    scene_files, scene_rows = [], []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid scene row {index}")
        expected_part = (index - 1) // scenes + 1
        expected_local = (index - 1) % scenes + 1
        try:
            part = int(row.get("part", 0))
            local = int(row.get("local_scene", row.get("scene", 0)))
            global_scene = int(row.get("global_scene", index))
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"Invalid scene mapping at row {index}") from exc
        if (part, local, global_scene) != (expected_part, expected_local, index):
            raise RuntimeError(f"Scene order mismatch at row {index}")
        if row.get("status") != "completed" or row.get("captions_burned") is not True:
            raise RuntimeError(f"Scene {index} incomplete or captions not burned")
        path = safe_path(row.get("video"), f"Scene {index}", 1000)
        require_av(path, f"Scene {index}")
        scene_files.append(path)
        scene_rows.append({
            "part": part, "local_scene": local,
            "global_scene": global_scene, "path": relative(path),
        })

    part_data = data.get("parts")
    if not isinstance(part_data, list) or len(part_data) != parts:
        raise RuntimeError(f"Expected {parts} part videos")
    part_files, part_rows = [], []
    for index, row in enumerate(part_data, 1):
        if isinstance(row, dict):
            value = row.get("path") or row.get("video") or row.get("output")
            if int(row.get("part", index)) != index:
                raise RuntimeError(f"Part order mismatch at row {index}")
            if row.get("status") not in (None, "completed"):
                raise RuntimeError(f"Part {index} is incomplete")
        else:
            value = row
        path = safe_path(value, f"Part video {index}", 1000)
        require_av(path, f"Part video {index}")
        part_files.append(path)
        part_rows.append({"part": index, "path": relative(path)})

    final_video = safe_path(data.get("final_video"), "Final video", 1000)
    alias = safe_path(data.get("alias"), "Format alias", 1000)
    require_av(final_video, "Final video")
    require_av(alias, "Format alias")

    expected_alias = "katha_lok_ai_short.mp4" if mode == "short" else "katha_lok_ai_full.mp4"
    if alias.name != expected_alias:
        raise RuntimeError(f"Expected alias {expected_alias}, got {alias.name}")
    if final_video.resolve() == alias.resolve():
        raise RuntimeError("Final video and format alias must be separate files")
    if abs(media_info(final_video)["duration"] - media_info(alias)["duration"]) > 1:
        raise RuntimeError("Final video and alias durations differ")

    return {
        "render_manifest": RENDER_MANIFEST,
        "scene_files": scene_files, "scene_rows": scene_rows,
        "part_files": part_files, "part_rows": part_rows,
        "final_video": final_video, "alias": alias,
    }


def validate_thumbnail():
    path = safe_path("output/thumbnail.jpg", "Thumbnail", 1000)
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            if image.size != THUMBNAIL_SIZE or image.format != "JPEG":
                raise RuntimeError(
                    f"Thumbnail must be a 1280x720 JPEG; got {image.size}, {image.format}"
                )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Invalid thumbnail: {exc}") from exc
    return path


def validate_support_files():
    metadata = safe_path("output/youtube_metadata.txt", "Metadata", 10)
    captions = safe_path("output/captions/final_captions.srt", "Final captions", 10)
    if "-->" not in captions.read_text(encoding="utf-8-sig"):
        raise RuntimeError("SRT has no timing cues")
    validation = read_json(VALIDATION_REPORT)
    return metadata, captions, validation


def curated_files(mode, render, thumbnail, metadata, captions):
    """Keep required deliverables and useful assets; reject stale other-format videos."""
    selected = {
        render["render_manifest"].resolve(),
        render["final_video"].resolve(),
        render["alias"].resolve(),
        thumbnail.resolve(), metadata.resolve(), captions.resolve(),
        VALIDATION_REPORT.resolve(),
    }
    selected.update(p.resolve() for p in render["scene_files"])
    selected.update(p.resolve() for p in render["part_files"])

    # Include supporting assets, manifests and audio/images, but not stale
    # final/alias videos belonging to the other format.
    excluded_video_names = {
        "katha_lok_ai_short.mp4" if mode == "full" else "katha_lok_ai_full.mp4"
    }
    ignored_dirs = {".git", "__pycache__", ".pytest_cache"}
    ignored_suffixes = {".zip", ".tmp", ".lock", ".pyc"}
    output_root = OUTPUT.resolve()

    for path in OUTPUT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in ignored_dirs for part in path.parts):
            continue
        if path.suffix.lower() in ignored_suffixes:
            continue
        if path.name.endswith(".concat.txt"):
            continue
        if path.name == REPORT.name:
            continue
        if path.name in excluded_video_names and path.resolve() not in selected:
            continue
        if not path.resolve().is_relative_to(output_root):
            raise RuntimeError(f"Unsafe output path: {path}")
        selected.add(path.resolve())

    files = []
    for path in sorted(selected, key=lambda p: p.as_posix()):
        if not path.is_file():
            raise RuntimeError(f"Selected package file missing: {path}")
        if not path.is_relative_to(output_root):
            raise RuntimeError(f"Selected file escapes output/: {path}")
        files.append(path)
    return files


def build_archive(files):
    temporary = ARCHIVE.with_name(ARCHIVE.name + ".tmp")
    temporary.unlink(missing_ok=True)
    entries = []
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED,
            compresslevel=6, allowZip64=True,
        ) as archive:
            for path in files:
                name = archive_name(path)
                archive.write(path, arcname=name)
                entries.append({
                    "path": name,
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                })

        with zipfile.ZipFile(temporary, "r") as archive:
            if archive.testzip():
                raise RuntimeError("ZIP integrity test failed")
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise RuntimeError("Duplicate ZIP entries found")
            if set(names) != {item["path"] for item in entries}:
                raise RuntimeError("ZIP contents differ from package manifest")
            for item in entries:
                payload = archive.read(item["path"])
                if len(payload) != item["bytes"] or sha256_bytes(payload) != item["sha256"]:
                    raise RuntimeError(f"ZIP content hash mismatch: {item['path']}")
        os.replace(temporary, ARCHIVE)
    finally:
        temporary.unlink(missing_ok=True)
    return entries


def reconcile(entries, render):
    entry_map = {item["path"]: item for item in entries}
    required = [("render_manifest", render["render_manifest"])]
    required.extend(("scene_video", OUTPUT / row["path"]) for row in render["scene_rows"])
    required.extend(("part_video", OUTPUT / row["path"]) for row in render["part_rows"])
    required.extend([
        ("final_video", render["final_video"]),
        ("format_alias", render["alias"]),
    ])

    verified = []
    with zipfile.ZipFile(ARCHIVE, "r") as archive:
        if archive.testzip():
            raise RuntimeError("ZIP failed integrity check during reconciliation")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise RuntimeError("Duplicate ZIP entries during reconciliation")
        for role, source in required:
            source = Path(source).resolve()
            name = archive_name(source)
            if name not in names or name not in entry_map:
                raise RuntimeError(f"Render-manifest file missing from ZIP: {name}")
            payload = archive.read(name)
            if len(payload) != source.stat().st_size:
                raise RuntimeError(f"ZIP/source size mismatch: {name}")
            digest = sha256_bytes(payload)
            if digest != sha256_file(source) or digest != entry_map[name]["sha256"]:
                raise RuntimeError(f"ZIP/source hash mismatch: {name}")
            verified.append({"role": role, "path": name, "sha256": digest, "verified": True})

    scene_count = sum(item["role"] == "scene_video" for item in verified)
    part_count = sum(item["role"] == "part_video" for item in verified)
    if scene_count != len(render["scene_files"]) or part_count != len(render["part_files"]):
        raise RuntimeError("Render manifest and ZIP entry counts differ")
    return {
        "status": "valid",
        "method": "ZIP entries compared with render manifest and source SHA-256",
        "scene_entries_verified": scene_count,
        "part_entries_verified": part_count,
        "verified_entry_count": len(verified),
        "files": verified,
    }


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required")
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = str(get_format(config)).strip().lower()
    parts, scenes = int(get_parts(config)), int(get_scenes(config))
    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported format: {mode}")
    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")
    if not OUTPUT.is_dir():
        raise RuntimeError("output/ directory missing")

    log("PACKAGING:", VERSION, "| FORMAT:", mode)
    validation = read_json(VALIDATION_REPORT)
    if validation.get("status") != "completed":
        raise RuntimeError("Final validation has not passed")
    if str(validation.get("format", "")).lower() != mode:
        raise RuntimeError("Validation report format mismatch")
    if int(validation.get("parts", -1)) != parts:
        raise RuntimeError("Validation report parts mismatch")
    if int(validation.get("scenes_per_part", -1)) != scenes:
        raise RuntimeError("Validation report scenes-per-part mismatch")
    if int(validation.get("expected_scenes", -1)) != parts * scenes:
        raise RuntimeError("Validation report expected scene count mismatch")

    checks = validation.get("checks", {})
    for name in ("photo_motion", "narration", "captions", "sound_design", "render", "thumbnail", "metadata"):
        if not isinstance(checks.get(name), dict) or checks[name].get("status") != "valid":
            raise RuntimeError(f"Final validation check invalid: {name}")

    render = validate_render(mode, parts, scenes)
    thumbnail = validate_thumbnail()
    metadata, captions, _ = validate_support_files()
    files = curated_files(mode, render, thumbnail, metadata, captions)

    required = {
        render["render_manifest"].resolve(), render["final_video"].resolve(),
        render["alias"].resolve(), thumbnail.resolve(), metadata.resolve(),
        captions.resolve(), VALIDATION_REPORT.resolve(),
        *(p.resolve() for p in render["scene_files"]),
        *(p.resolve() for p in render["part_files"]),
    }
    included = {p.resolve() for p in files}
    missing = required - included
    if missing:
        raise RuntimeError(f"Essential deliverables excluded: {sorted(map(str, missing))}")

    entries = build_archive(files)
    reconciliation = reconcile(entries, render)

    for path in files:
        entry = next(item for item in entries if item["path"] == archive_name(path))
        if not path.is_file() or path.stat().st_size != entry["bytes"] or sha256_file(path) != entry["sha256"]:
            raise RuntimeError(f"Source changed after ZIP creation: {path}")

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": parts,
        "scenes_per_part": scenes,
        "scene_count": parts * scenes,
        "video_dimensions": list(VIDEO_SIZE),
        "thumbnail_dimensions": list(THUMBNAIL_SIZE),
        "validation_report": {
            "path": "output/validation_report.json",
            "status": validation.get("status"),
            "version": validation.get("version"),
        },
        "archive": ARCHIVE.name,
        "archive_bytes": ARCHIVE.stat().st_size,
        "archive_sha256": sha256_file(ARCHIVE),
        "file_count": len(entries),
        "files": entries,
        "render_manifest_reconciliation": reconciliation,
        "deliverables": {
            "render_manifest": render["render_manifest"].relative_to(ROOT).as_posix(),
            "final_video": render["final_video"].relative_to(ROOT).as_posix(),
            "alias": render["alias"].relative_to(ROOT).as_posix(),
            "thumbnail": thumbnail.relative_to(ROOT).as_posix(),
            "metadata": metadata.relative_to(ROOT).as_posix(),
            "captions": captions.relative_to(ROOT).as_posix(),
            "part_videos": [p.relative_to(ROOT).as_posix() for p in render["part_files"]],
            "scene_videos": [p.relative_to(ROOT).as_posix() for p in render["scene_files"]],
        },
        "render_manifest_reconciliation_status": reconciliation["status"],
        "external_api_calls_during_packaging": 0,
    }
    atomic_json(REPORT, report)
    log("PACKAGING: PASSED")
    log("ARCHIVE:", ARCHIVE.name)
    log("FILES:", len(entries))
    log("SCENE ZIP RECONCILIATION:", reconciliation["scene_entries_verified"])
    log("PART ZIP RECONCILIATION:", reconciliation["part_entries_verified"])
    log("REPORT:", REPORT.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("PACKAGING: FAILED:", str(exc))
        sys.exit(1)
