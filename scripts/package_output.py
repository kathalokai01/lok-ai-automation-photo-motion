
#!/usr/bin/env python3
"""Validate and safely package Katha Lok AI photo-motion deliverables."""

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

sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes

VERSION = "output-packager-v4-strict-sfx-validation"
MIN_VIDEO_BYTES = 1000
MIN_IMAGE_BYTES = 1000
MIN_TEXT_BYTES = 10

RENDER_MANIFEST = OUTPUT / "scene_renders" / "render_manifest.json"
SOUND_MANIFEST = OUTPUT / "sound_design" / "sound_design_manifest.json"
CUE_MANIFEST = OUTPUT / "sound_design" / "sfx_cues.json"
SFX_REPORT = OUTPUT / "sound_design" / "sfx_timing_validation.json"
CAPTIONS = OUTPUT / "captions" / "final_captions.srt"
THUMBNAIL = OUTPUT / "thumbnail.jpg"
METADATA = OUTPUT / "youtube_metadata.txt"


def log(*args):
    print(*args, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    if not path.is_file():
        raise RuntimeError(f"Required JSON file missing: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON file {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object: {path}")

    return data


def require_file(path, label, minimum_bytes=1):
    if not path.is_file():
        raise RuntimeError(f"{label} missing: {path}")
    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(
            f"{label} is too small ({path.stat().st_size} bytes): {path}"
        )
    return path


def safe_output_file(value, label, minimum_bytes=MIN_VIDEO_BYTES):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: file path is missing")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    if not path.is_relative_to(OUTPUT.resolve()):
        raise RuntimeError(f"{label}: path escapes output/: {path}")

    return require_file(path, label, minimum_bytes)


def media_info(path):
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is required for packaging")

    result = subprocess.run(
        [
            executable, "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-800:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path.name}") from exc

    if duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path.name}")

    streams = [
        item for item in data.get("streams", [])
        if isinstance(item, dict)
    ]
    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        None,
    )

    return {
        "duration": duration,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
    }


def require_media(path, label, video=False, audio=False):
    info = media_info(path)

    if video and not info["has_video"]:
        raise RuntimeError(f"{label}: video stream missing")
    if audio and not info["has_audio"]:
        raise RuntimeError(f"{label}: audio stream missing")

    return info


def expected_keys(parts, scenes):
    return {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }


def validate_render(mode, parts, scenes):
    data = read_json(RENDER_MANIFEST)

    if data.get("status") != "completed":
        raise RuntimeError("Render manifest is not completed")
    if data.get("format") != mode:
        raise RuntimeError("Render format differs from Input/topic.txt")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Render manifest does not confirm burned captions")

    rows = data.get("completed_scenes")
    expected_count = parts * scenes

    if not isinstance(rows, list) or len(rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} completed scenes; "
            f"found {len(rows) if isinstance(rows, list) else 0}"
        )
    if int(data.get("completed_count", -1)) != expected_count:
        raise RuntimeError("Render completed_count mismatch")

    scene_files = []

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid render row {index}")

        expected_part = (index - 1) // scenes + 1
        expected_scene = (index - 1) % scenes + 1

        try:
            part = int(row.get("part", 0))
            scene = int(row.get("local_scene", row.get("scene", 0)))
            global_scene = int(row.get("global_scene", index))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid scene numbering at row {index}") from exc

        if (part, scene, global_scene) != (
            expected_part, expected_scene, index
        ):
            raise RuntimeError(f"Scene order mismatch at row {index}")

        if row.get("status") != "completed":
            raise RuntimeError(f"Scene {index} is not completed")
        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Scene {index} lacks caption confirmation")

        video = safe_output_file(row.get("video"), f"Captioned scene {index}")
        info = require_media(
            video, f"Captioned scene {index}", video=True, audio=True
        )

        expected_resolution = (
            (720, 1280) if mode == "short" else (1280, 720)
        )
        if (info["width"], info["height"]) != expected_resolution:
            raise RuntimeError(
                f"Scene {index} resolution mismatch: "
                f"{info['width']}x{info['height']}"
            )

        scene_files.append(video)

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        raise RuntimeError(f"Expected exactly {parts} part videos")

    part_files = []
    for index, value in enumerate(part_rows, start=1):
        video = safe_output_file(value, f"Part video {index}")
        require_media(video, f"Part video {index}", video=True, audio=True)
        part_files.append(video)

    final_video = safe_output_file(data.get("final_video"), "Final video")
    final_info = require_media(
        final_video, "Final video", video=True, audio=True
    )

    alias = safe_output_file(data.get("alias"), "Format-specific video")
    require_media(alias, "Format-specific video", video=True, audio=True)

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias.name != expected_alias:
        raise RuntimeError(f"Expected alias {expected_alias}, got {alias.name}")

    opposite_alias = (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    if (OUTPUT / "videos" / opposite_alias).exists():
        raise RuntimeError(f"Unexpected opposite-format video: {opposite_alias}")

    return {
        "scene_files": scene_files,
        "part_files": part_files,
        "final_video": final_video,
        "alias": alias,
        "final_duration": final_info["duration"],
    }


def validate_sound_design(parts, scenes):
    sound = read_json(SOUND_MANIFEST)
    cues = read_json(CUE_MANIFEST)
    timing = read_json(SFX_REPORT)

    if sound.get("status") != "completed":
        raise RuntimeError("Sound-design manifest is incomplete")
    if cues.get("status") != "completed":
        raise RuntimeError("SFX cue manifest is incomplete")
    if timing.get("status") != "completed":
        raise RuntimeError("SFX timing validation report is incomplete")
    if timing.get("external_api_calls", 0) != 0:
        raise RuntimeError("SFX timing report declares external API calls")
    if timing.get("paid_services_used") is True:
        raise RuntimeError("SFX timing report declares paid-service usage")

    sfx_report = timing.get("sfx")
    if not isinstance(sfx_report, dict) or sfx_report.get("status") != "valid":
        raise RuntimeError("SFX timing report does not show valid status")

    expected_count = parts * scenes
    if int(timing.get("expected_scenes", -1)) != expected_count:
        raise RuntimeError("SFX timing report scene count mismatch")
    if int(timing.get("narration_scenes_checked", -1)) != expected_count:
        raise RuntimeError("SFX timing report did not check every narration scene")

    cue_rows = cues.get("cues", cues.get("items", []))
    if not isinstance(cue_rows, list):
        raise RuntimeError("SFX cue list is invalid")

    expected = expected_keys(parts, scenes)
    for index, cue in enumerate(cue_rows, start=1):
        if not isinstance(cue, dict):
            raise RuntimeError(f"Invalid SFX cue {index}")

        try:
            key = (int(cue["part"]), int(cue["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid SFX scene mapping at cue {index}") from exc

        if key not in expected:
            raise RuntimeError(f"SFX cue references unknown scene: {key}")

        path_value = (
            cue.get("file") or cue.get("path")
            or cue.get("audio") or cue.get("output")
        )
        if not isinstance(path_value, str) or not path_value.strip():
            raise RuntimeError(f"SFX cue {index} has no audio path")

        audio = safe_output_file(
            path_value, f"SFX cue {index}", minimum_bytes=100
        )
        require_media(audio, f"SFX cue {index}", audio=True)

    tracks = sound.get("tracks", {})
    enabled = sound.get("enabled", {})
    if not isinstance(tracks, dict) or not isinstance(enabled, dict):
        raise RuntimeError("Invalid sound-design track structure")

    track_report = {}
    for name in ("music", "ambient", "sfx"):
        track = tracks.get(name)
        is_enabled = bool(enabled.get(name, track is not None))

        if not is_enabled:
            track_report[name] = "disabled"
            continue
        if not isinstance(track, dict):
            raise RuntimeError(f"Enabled sound track missing: {name}")
        if track.get("status") != "completed":
            raise RuntimeError(f"Sound track incomplete: {name}")

        value = track.get("path") or track.get("file") or track.get("output")
        if not value:
            raise RuntimeError(f"Sound track has no path: {name}")

        path = safe_output_file(value, f"Sound track {name}", 100)
        require_media(path, f"Sound track {name}", audio=True)
        track_report[name] = "valid"

    return {
        "cue_count": len(cue_rows),
        "tracks": track_report,
        "timing_report": SFX_REPORT.relative_to(ROOT).as_posix(),
    }


def validate_other_files():
    thumbnail = require_file(THUMBNAIL, "Thumbnail", MIN_IMAGE_BYTES)
    metadata = require_file(METADATA, "YouTube metadata", MIN_TEXT_BYTES)
    captions = require_file(CAPTIONS, "Final captions SRT", MIN_TEXT_BYTES)

    content = captions.read_text(encoding="utf-8-sig")
    if "-->" not in content:
        raise RuntimeError("Final captions SRT has no timing cues")

    return {
        "thumbnail": thumbnail,
        "metadata": metadata,
        "captions": captions,
    }


def excluded_from_archive(path):
    relative = path.relative_to(OUTPUT)

    if path == REPORT:
        return True
    if path.suffix.lower() == ".zip":
        return True
    if path.name.endswith((".tmp", ".tmp.mp4", ".concat.txt", ".lock")):
        return True
    if path.name in {".DS_Store", "Thumbs.db"}:
        return True
    if "__pycache__" in relative.parts:
        return True

    return False


def build_file_list():
    root = OUTPUT.resolve()
    files = []

    for path in OUTPUT.rglob("*"):
        if not path.is_file() or excluded_from_archive(path):
            continue

        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise RuntimeError(f"Refusing path outside output/: {path}")

        files.append(path)

    return sorted(
        files,
        key=lambda path: path.relative_to(OUTPUT).as_posix(),
    )


def write_archive(files):
    temporary = ARCHIVE.with_name(ARCHIVE.name + ".tmp")
    temporary.unlink(missing_ok=True)
    entries = []

    try:
        with zipfile.ZipFile(
            temporary, "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for path in files:
                name = f"output/{path.relative_to(OUTPUT).as_posix()}"
                archive.write(path, arcname=name)
                entries.append({
                    "path": name,
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                })

        with zipfile.ZipFile(temporary, "r") as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise RuntimeError(f"ZIP integrity error: {bad_member}")

            names = archive.namelist()
            if len(names) != len(set(names)):
                raise RuntimeError("Duplicate ZIP entries found")
            if set(names) != {entry["path"] for entry in entries}:
                raise RuntimeError("ZIP entries do not match the file list")

            for entry in entries:
                data = archive.read(entry["path"])
                if len(data) != entry["bytes"]:
                    raise RuntimeError(f"Archived size mismatch: {entry['path']}")
                if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    raise RuntimeError(f"Archived hash mismatch: {entry['path']}")

        os.replace(temporary, ARCHIVE)
    finally:
        temporary.unlink(missing_ok=True)

    return entries


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


def main():
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported format: {mode}")
    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")
    if not OUTPUT.is_dir():
        raise RuntimeError("output/ directory does not exist")

    # Refuse packaging until all final deliverables and SFX timing are valid.
    render = validate_render(mode, parts, scenes)
    other = validate_other_files()
    sound = validate_sound_design(parts, scenes)

    files = build_file_list()
    if not files:
        raise RuntimeError("No output files available for packaging")

    essential = [
        render["final_video"],
        render["alias"],
        other["thumbnail"],
        other["metadata"],
        other["captions"],
        *render["part_files"],
        *render["scene_files"],
    ]
    file_set = {path.resolve() for path in files}

    for path in essential:
        if path.resolve() not in file_set:
            raise RuntimeError(f"Essential file excluded from ZIP: {path}")

    entries = write_archive(files)

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": parts,
        "scenes_per_part": scenes,
        "scene_count": parts * scenes,
        "archive": ARCHIVE.name,
        "archive_bytes": ARCHIVE.stat().st_size,
        "archive_sha256": sha256_file(ARCHIVE),
        "file_count": len(entries),
        "files": entries,
        "manifest_in_archive": False,
        "deliverables": {
            "final_video": render["final_video"].relative_to(ROOT).as_posix(),
            "format_alias": render["alias"].relative_to(ROOT).as_posix(),
            "thumbnail": other["thumbnail"].relative_to(ROOT).as_posix(),
            "metadata": other["metadata"].relative_to(ROOT).as_posix(),
            "captions": other["captions"].relative_to(ROOT).as_posix(),
        },
        "sound_design": sound,
        "notes": [
            "ZIP integrity and every archived file hash were verified.",
            "SFX timing validation must pass before packaging.",
            "The package manifest is outside the ZIP to avoid a self-referential checksum.",
        ],
    }

    atomic_write_json(REPORT, report)

    log("PACKAGING COMPLETE")
    log("FORMAT:", mode)
    log("SCENES:", parts * scenes)
    log("FILES:", len(entries))
    log("ZIP:", ARCHIVE.name)
    log("ZIP BYTES:", ARCHIVE.stat().st_size)
    log("ZIP SHA256:", report["archive_sha256"])
    log("REPORT:", REPORT.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("PACKAGING FAILED:", str(exc))
        sys.exit(1)
