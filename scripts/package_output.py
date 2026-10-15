
#!/usr/bin/env python3
"""Package only successfully validated Katha Lok AI video deliverables."""

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

VERSION = "output-packager-v6-youtube-16x9"

sys.path.insert(0, str(ROOT / "scripts"))
from input_config import load_and_validate, get_format, get_parts, get_scenes

MIN_VIDEO_BYTES = 1000
MIN_IMAGE_BYTES = 1000
MIN_TEXT_BYTES = 10
EXPECTED_VIDEO_SIZE = (1280, 720)
EXPECTED_THUMBNAIL_SIZE = (1280, 720)


def log(*values):
    print(*values, flush=True)


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
        raise RuntimeError(f"{label} is too small: {path}")
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
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required for packaging")

    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json",
            str(path),
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
        raise RuntimeError(f"Invalid duration: {path.name}")

    streams = data.get("streams", [])
    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"),
        None,
    )

    return {
        "duration": duration,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
    }


def require_av(path, label, mode=None):
    info = media_info(path)

    if not info["has_video"]:
        raise RuntimeError(f"{label}: video stream missing")
    if not info["has_audio"]:
        raise RuntimeError(f"{label}: audio stream missing")

    if mode:
        actual = (info["width"], info["height"])
        if actual != EXPECTED_VIDEO_SIZE:
            raise RuntimeError(
                f"{label}: expected "
                f"{EXPECTED_VIDEO_SIZE[0]}x{EXPECTED_VIDEO_SIZE[1]}, "
                f"got {actual[0]}x{actual[1]}"
            )

    return info


def require_validation(mode, parts, scenes):
    data = read_json(VALIDATION_REPORT)

    if data.get("status") != "completed":
        raise RuntimeError(
            "Final validation has not passed. Packaging is blocked."
        )

    if data.get("format") != mode:
        raise RuntimeError("Validation report format differs from input")
    if int(data.get("parts", -1)) != parts:
        raise RuntimeError("Validation report part count mismatch")
    if int(data.get("scenes_per_part", -1)) != scenes:
        raise RuntimeError("Validation report scenes-per-part mismatch")
    if int(data.get("expected_scenes", -1)) != parts * scenes:
        raise RuntimeError("Validation report expected scene count mismatch")

    checks = data.get("checks")
    if not isinstance(checks, dict):
        raise RuntimeError("Validation report has no checks object")

    required_checks = (
        "photo_motion",
        "narration",
        "captions",
        "sound_design",
        "render",
        "thumbnail",
        "metadata",
    )

    for key in required_checks:
        check = checks.get(key)
        if not isinstance(check, dict) or check.get("status") != "valid":
            raise RuntimeError(
                f"Final validation check is missing or invalid: {key}"
            )

    return data


def validate_render(mode, parts, scenes):
    manifest_path = OUTPUT / "scene_renders" / "render_manifest.json"
    data = read_json(manifest_path)

    if data.get("status") != "completed":
        raise RuntimeError("Render manifest is incomplete")
    if data.get("format") != mode:
        raise RuntimeError("Render format mismatch")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Captions are not confirmed as burned")

    rows = data.get("completed_scenes")
    expected_count = parts * scenes

    if not isinstance(rows, list) or len(rows) != expected_count:
        count = len(rows) if isinstance(rows, list) else 0
        raise RuntimeError(
            f"Expected {expected_count} rendered scenes; found {count}"
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
            raise RuntimeError(
                f"Invalid scene mapping at row {index}"
            ) from exc

        if (part, scene, global_scene) != (
            expected_part, expected_scene, index
        ):
            raise RuntimeError(f"Scene order mismatch at row {index}")

        if row.get("status") != "completed":
            raise RuntimeError(f"Scene {index} is incomplete")
        if row.get("captions_burned") is not True:
            raise RuntimeError(
                f"Scene {index} captions are not confirmed"
            )

        path = safe_output_file(row.get("video"), f"Scene {index}")
        require_av(path, f"Scene {index}", mode)
        scene_files.append(path)

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        raise RuntimeError(f"Expected {parts} part videos")

    part_files = []

    for index, value in enumerate(part_rows, start=1):
        if isinstance(value, dict):
            path_value = (
                value.get("path")
                or value.get("video")
                or value.get("output")
            )
        else:
            path_value = value

        path = safe_output_file(path_value, f"Part video {index}")
        require_av(path, f"Part video {index}", mode)
        part_files.append(path)

    final_video = safe_output_file(data.get("final_video"), "Final video")
    alias = safe_output_file(data.get("alias"), "Format-specific video")

    final_info = require_av(final_video, "Final video", mode)
    alias_info = require_av(alias, "Format-specific video", mode)

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )

    if alias.name != expected_alias:
        raise RuntimeError(
            f"Expected {expected_alias}, got {alias.name}"
        )

    if abs(final_info["duration"] - alias_info["duration"]) > 1.0:
        raise RuntimeError("Final video and format alias durations differ")

    return {
        "scene_files": scene_files,
        "part_files": part_files,
        "final_video": final_video,
        "alias": alias,
        "duration": final_info["duration"],
    }


def validate_thumbnail(mode):
    path = require_file(
        OUTPUT / "thumbnail.jpg",
        "Thumbnail",
        MIN_IMAGE_BYTES,
    )

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            actual = image.size
            image_format = image.format

    except Exception as exc:
        raise RuntimeError(f"Thumbnail is invalid: {exc}") from exc

    if actual != EXPECTED_THUMBNAIL_SIZE:
        raise RuntimeError(
            f"Thumbnail dimensions mismatch: expected "
            f"{EXPECTED_THUMBNAIL_SIZE}, got {actual}"
        )

    if image_format != "JPEG":
        raise RuntimeError(f"Expected JPEG thumbnail, got {image_format}")

    return path


def validate_metadata():
    path = require_file(
        OUTPUT / "youtube_metadata.txt",
        "YouTube metadata",
        MIN_TEXT_BYTES,
    )

    content = path.read_text(encoding="utf-8-sig").strip()
    if len(content) < MIN_TEXT_BYTES:
        raise RuntimeError("YouTube metadata is empty")

    return path


def validate_sound_design(parts, scenes):
    sound = read_json(
        OUTPUT / "sound_design" / "sound_design_manifest.json"
    )
    cues = read_json(OUTPUT / "sound_design" / "sfx_cues.json")
    timing = read_json(
        OUTPUT / "sound_design" / "sfx_timing_validation.json"
    )

    if sound.get("status") != "completed":
        raise RuntimeError("Sound-design manifest is incomplete")
    if cues.get("status") != "completed":
        raise RuntimeError("SFX cue manifest is incomplete")
    if timing.get("status") != "completed":
        raise RuntimeError("SFX timing report is incomplete")
    if timing.get("external_api_calls", 0) != 0:
        raise RuntimeError("SFX report declares external API calls")
    if timing.get("paid_services_used") is True:
        raise RuntimeError("SFX report declares paid-service usage")

    sfx = timing.get("sfx")
    if not isinstance(sfx, dict) or sfx.get("status") != "valid":
        raise RuntimeError("SFX timing report does not show valid status")

    expected_count = parts * scenes

    if int(timing.get("expected_scenes", -1)) != expected_count:
        raise RuntimeError("SFX expected scene count mismatch")
    if int(timing.get("narration_scenes_checked", -1)) != expected_count:
        raise RuntimeError(
            "SFX report did not check every narration scene"
        )

    cue_rows = cues.get("cues", cues.get("items", []))
    if not isinstance(cue_rows, list):
        raise RuntimeError("SFX cue list is invalid")

    for index, cue in enumerate(cue_rows, start=1):
        if not isinstance(cue, dict):
            raise RuntimeError(f"Invalid SFX cue {index}")

        path_value = (
            cue.get("file")
            or cue.get("path")
            or cue.get("audio")
            or cue.get("output")
        )

        path = safe_output_file(
            path_value,
            f"SFX cue {index}",
            minimum_bytes=100,
        )

        info = media_info(path)
        if not info["has_audio"]:
            raise RuntimeError(f"SFX cue {index} has no audio stream")

    tracks = sound.get("tracks", {})
    enabled = sound.get("enabled", {})

    if not isinstance(tracks, dict) or not isinstance(enabled, dict):
        raise RuntimeError("Invalid sound-design tracks structure")

    for name in ("music", "ambient", "sfx"):
        track = tracks.get(name)

        if not bool(enabled.get(name, track is not None)):
            continue

        if not isinstance(track, dict) or track.get("status") != "completed":
            raise RuntimeError(f"Enabled sound track is incomplete: {name}")

        value = (
            track.get("path")
            or track.get("file")
            or track.get("output")
        )

        path = safe_output_file(value, f"Sound track {name}", 100)
        info = media_info(path)

        if not info["has_audio"]:
            raise RuntimeError(f"Sound track has no audio stream: {name}")

    return {
        "cue_count": len(cue_rows),
        "timing_report": (
            "output/sound_design/sfx_timing_validation.json"
        ),
    }


def archive_files():
    files = []

    for path in OUTPUT.rglob("*"):
        if not path.is_file():
            continue
        if path.resolve() == REPORT.resolve():
            continue
        if path.suffix.lower() == ".zip":
            continue
        if path.name.endswith((".tmp", ".lock", ".concat.txt")):
            continue
        if "__pycache__" in path.parts:
            continue

        resolved = path.resolve()

        if not resolved.is_relative_to(OUTPUT.resolve()):
            raise RuntimeError(f"Refusing path outside output/: {path}")

        files.append(path)

    return sorted(
        files,
        key=lambda path: path.relative_to(OUTPUT).as_posix(),
    )


def build_archive(files):
    temporary = ARCHIVE.with_name(ARCHIVE.name + ".tmp")
    temporary.unlink(missing_ok=True)
    entries = []

    try:
        with zipfile.ZipFile(
            temporary,
            "w",
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
            bad = archive.testzip()
            if bad:
                raise RuntimeError(f"ZIP integrity check failed: {bad}")

            names = archive.namelist()

            if len(names) != len(set(names)):
                raise RuntimeError("Duplicate ZIP entries detected")

            if set(names) != {row["path"] for row in entries}:
                raise RuntimeError("ZIP contents do not match file manifest")

            for row in entries:
                content = archive.read(row["path"])

                if len(content) != row["bytes"]:
                    raise RuntimeError(f"ZIP size mismatch: {row['path']}")

                if hashlib.sha256(content).hexdigest() != row["sha256"]:
                    raise RuntimeError(f"ZIP hash mismatch: {row['path']}")

        os.replace(temporary, ARCHIVE)

    finally:
        temporary.unlink(missing_ok=True)

    return entries


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
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported format: {mode}")
    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")
    if not OUTPUT.is_dir():
        raise RuntimeError("output/ directory does not exist")

    log("PACKAGING AUDIT:", VERSION)
    log("FORMAT:", mode)
    log("EXPECTED VIDEO SIZE:", "1280x720")
    log("EXPECTED THUMBNAIL SIZE:", "1280x720")

    # Fail closed: no package is produced from a missing or failed report.
    validation = require_validation(mode, parts, scenes)

    render = validate_render(mode, parts, scenes)
    thumbnail = validate_thumbnail(mode)
    metadata = validate_metadata()
    sound = validate_sound_design(parts, scenes)

    captions = require_file(
        OUTPUT / "captions" / "final_captions.srt",
        "Final captions SRT",
        MIN_TEXT_BYTES,
    )

    if "-->" not in captions.read_text(encoding="utf-8-sig"):
        raise RuntimeError("Final SRT contains no timing cues")

    files = archive_files()
    if not files:
        raise RuntimeError("No output files available to package")

    included = {path.resolve() for path in files}

    essential = [
        render["final_video"],
        render["alias"],
        thumbnail,
        metadata,
        captions,
        *render["part_files"],
        *render["scene_files"],
    ]

    for path in essential:
        if path.resolve() not in included:
            raise RuntimeError(f"Essential deliverable excluded: {path}")

    entries = build_archive(files)

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": parts,
        "scenes_per_part": scenes,
        "scene_count": parts * scenes,
        "video_dimensions": list(EXPECTED_VIDEO_SIZE),
        "thumbnail_dimensions": list(EXPECTED_THUMBNAIL_SIZE),
        "validation_report": {
            "path": "output/validation_report.json",
            "status": validation["status"],
            "version": validation.get("version"),
        },
        "archive": ARCHIVE.name,
        "archive_bytes": ARCHIVE.stat().st_size,
        "archive_sha256": sha256_file(ARCHIVE),
        "file_count": len(entries),
        "files": entries,
        "deliverables": {
            "final_video": render["final_video"].relative_to(ROOT).as_posix(),
            "format_alias": render["alias"].relative_to(ROOT).as_posix(),
            "thumbnail": thumbnail.relative_to(ROOT).as_posix(),
            "metadata": metadata.relative_to(ROOT).as_posix(),
            "captions": captions.relative_to(ROOT).as_posix(),
        },
        "sound_design": sound,
        "notes": [
            "Packaging is blocked unless final validation has passed.",
            "Short and Full outputs use 1280x720 video and thumbnail dimensions.",
            "Final videos must contain both video and audio streams.",
            "Every archived file has a verified size and SHA-256.",
            "The package manifest stays outside the ZIP to avoid a circular hash.",
        ],
    }

    atomic_json(REPORT, report)

    log("PACKAGING COMPLETE")
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
