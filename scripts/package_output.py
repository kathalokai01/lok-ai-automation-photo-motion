
#!/usr/bin/env python3
"""Safely package validated Katha Lok AI photo-motion deliverables."""

from __future__ import annotations

import hashlib
import json
import math
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

VERSION = "output-packager-v8-zip-render-reconciliation"
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


def sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def read_json(path):
    if not path.is_file():
        raise RuntimeError(f"Required JSON file missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected JSON object: {path}")
    return value


def strict_bool(value, label, default=None):
    if value is None:
        if default is not None:
            return default
        raise RuntimeError(f"{label}: missing boolean value")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "1", "on", "enabled"}:
        return True
    if normalized in {"false", "no", "0", "off", "disabled"}:
        return False
    raise RuntimeError(f"{label}: invalid boolean {value!r}")


def require_file(path, label, minimum=1):
    if not path.is_file():
        raise RuntimeError(f"{label} missing: {path}")
    if path.stat().st_size < minimum:
        raise RuntimeError(f"{label} is too small: {path}")
    return path


def safe_output_file(value, label, minimum=1000):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: file path missing")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    if not path.is_relative_to(OUTPUT.resolve()):
        raise RuntimeError(f"{label}: path escapes output/: {path}")

    return require_file(path, label, minimum)


def output_relative(path):
    """Return a safe normalized path relative to output/."""
    resolved = Path(path).resolve()
    output_root = OUTPUT.resolve()

    if not resolved.is_relative_to(output_root):
        raise RuntimeError(f"Path escapes output/: {resolved}")

    relative = resolved.relative_to(output_root).as_posix()
    if not relative or relative.startswith("../"):
        raise RuntimeError(f"Invalid output-relative path: {relative}")

    return relative


def archive_name(path):
    return "output/" + output_relative(path)


def media_info(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required for packaging")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
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
            f"ffprobe failed for {path.name}: {result.stderr[-600:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid media metadata: {path.name}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path.name}")

    streams = data.get("streams", [])
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


def require_av(path, label, resolution=VIDEO_SIZE):
    info = media_info(path)

    if not info["has_video"]:
        raise RuntimeError(f"{label}: video stream missing")
    if not info["has_audio"]:
        raise RuntimeError(f"{label}: audio stream missing")

    actual = (info["width"], info["height"])
    if actual != resolution:
        raise RuntimeError(
            f"{label}: expected {resolution[0]}x{resolution[1]}, "
            f"got {actual[0]}x{actual[1]}"
        )

    return info


def require_validation(mode, parts, scenes):
    data = read_json(VALIDATION_REPORT)

    if data.get("status") != "completed":
        raise RuntimeError("Final validation has not passed")
    if data.get("format") != mode:
        raise RuntimeError("Validation report format mismatch")
    if int(data.get("parts", -1)) != parts:
        raise RuntimeError("Validation report part count mismatch")
    if int(data.get("scenes_per_part", -1)) != scenes:
        raise RuntimeError("Validation report scenes-per-part mismatch")
    if int(data.get("expected_scenes", -1)) != parts * scenes:
        raise RuntimeError("Validation report expected scene count mismatch")

    checks = data.get("checks")
    if not isinstance(checks, dict):
        raise RuntimeError("Validation report has no checks object")

    for name in (
        "photo_motion", "narration", "captions", "sound_design",
        "render", "thumbnail", "metadata",
    ):
        check = checks.get(name)
        if not isinstance(check, dict) or check.get("status") != "valid":
            raise RuntimeError(f"Final validation check invalid: {name}")

    return data


def validate_render(mode, parts, scenes):
    data = read_json(RENDER_MANIFEST)

    if data.get("status") != "completed":
        raise RuntimeError("Render manifest is incomplete")
    if data.get("format") != mode:
        raise RuntimeError("Render format mismatch")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Captions are not confirmed as burned")

    expected_count = parts * scenes
    scene_rows = data.get("completed_scenes")

    if not isinstance(scene_rows, list) or len(scene_rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} rendered scenes; "
            f"found {len(scene_rows) if isinstance(scene_rows, list) else 0}"
        )

    if int(data.get("completed_count", -1)) != expected_count:
        raise RuntimeError("Render completed_count mismatch")

    scene_files = []
    scene_mappings = []

    for index, row in enumerate(scene_rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid render row {index}")

        expected_part = (index - 1) // scenes + 1
        expected_scene = (index - 1) % scenes + 1

        try:
            part = int(row.get("part", 0))
            scene = int(row.get("local_scene", row.get("scene", 0)))
            global_scene = int(row.get("global_scene", index))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid scene mapping: {index}") from exc

        if (part, scene, global_scene) != (
            expected_part, expected_scene, index
        ):
            raise RuntimeError(f"Scene order mismatch at row {index}")

        if row.get("status") != "completed":
            raise RuntimeError(f"Scene {index} incomplete")
        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Scene {index} captions not confirmed")

        path = safe_output_file(row.get("video"), f"Scene {index}")
        require_av(path, f"Scene {index}")

        scene_files.append(path)
        scene_mappings.append({
            "part": part,
            "local_scene": scene,
            "global_scene": global_scene,
            "path": output_relative(path),
        })

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        raise RuntimeError(f"Expected {parts} part videos")

    part_files = []
    part_mappings = []

    for index, row in enumerate(part_rows, 1):
        if isinstance(row, dict):
            value = row.get("path") or row.get("video") or row.get("output")
            row_part = row.get("part", index)
            try:
                row_part = int(row_part)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid part mapping: {index}") from exc
            if row_part != index:
                raise RuntimeError(f"Part order mismatch at row {index}")
            if row.get("status") not in (None, "completed"):
                raise RuntimeError(f"Part {index} is not completed")
        else:
            value = row

        path = safe_output_file(value, f"Part video {index}")
        require_av(path, f"Part video {index}")
        part_files.append(path)
        part_mappings.append({
            "part": index,
            "path": output_relative(path),
        })

    final_video = safe_output_file(data.get("final_video"), "Final video")
    alias = safe_output_file(data.get("alias"), "Format-specific video")

    final_info = require_av(final_video, "Final video")
    alias_info = require_av(alias, "Format-specific video")

    expected_alias = (
        "katha_lok_ai_short.mp4" if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias.name != expected_alias:
        raise RuntimeError(f"Expected {expected_alias}, got {alias.name}")

    if final_video.resolve() == alias.resolve():
        raise RuntimeError("Final video and format-specific alias must be distinct files")

    if abs(final_info["duration"] - alias_info["duration"]) > 1.0:
        raise RuntimeError("Final video and alias durations differ")

    return {
        "render_manifest": RENDER_MANIFEST,
        "scene_files": scene_files,
        "scene_mappings": scene_mappings,
        "part_files": part_files,
        "part_mappings": part_mappings,
        "final_video": final_video,
        "alias": alias,
        "duration": final_info["duration"],
    }


def validate_thumbnail():
    path = require_file(OUTPUT / "thumbnail.jpg", "Thumbnail", 1000)

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            dimensions = image.size
            image_format = image.format
    except Exception as exc:
        raise RuntimeError(f"Thumbnail is invalid: {exc}") from exc

    if dimensions != THUMBNAIL_SIZE:
        raise RuntimeError(
            f"Thumbnail must be {THUMBNAIL_SIZE}; got {dimensions}"
        )
    if image_format != "JPEG":
        raise RuntimeError(f"Expected JPEG thumbnail, got {image_format}")

    return path


def validate_metadata():
    path = require_file(OUTPUT / "youtube_metadata.txt", "YouTube metadata", 10)

    if len(path.read_text(encoding="utf-8-sig").strip()) < 10:
        raise RuntimeError("YouTube metadata is empty")

    return path


def validate_sound_design(parts, scenes):
    sound = read_json(OUTPUT / "sound_design" / "sound_design_manifest.json")
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
        raise RuntimeError("SFX timing report is not valid")

    expected = parts * scenes
    if int(timing.get("expected_scenes", -1)) != expected:
        raise RuntimeError("SFX expected scene count mismatch")
    if int(timing.get("narration_scenes_checked", -1)) != expected:
        raise RuntimeError("SFX narration scene count mismatch")

    cue_rows = cues.get("cues", cues.get("items", []))
    if not isinstance(cue_rows, list):
        raise RuntimeError("SFX cue list is invalid")

    enabled = sound.get("enabled", {})
    tracks = sound.get("tracks", {})

    if not isinstance(enabled, dict) or not isinstance(tracks, dict):
        raise RuntimeError("Invalid sound-design track settings")

    cue_enabled = strict_bool(
        enabled.get("sfx"),
        "sound_design.enabled.sfx",
        default=bool(cue_rows),
    )

    if cue_enabled and not cue_rows:
        raise RuntimeError("SFX enabled but no cues exist")
    if not cue_enabled and cue_rows:
        raise RuntimeError("SFX disabled but cues remain")

    for index, cue in enumerate(cue_rows, 1):
        if not isinstance(cue, dict):
            raise RuntimeError(f"Invalid SFX cue {index}")

        path_value = (
            cue.get("file") or cue.get("path") or cue.get("audio")
            or cue.get("output") or cue.get("audio_path")
        )
        path = safe_output_file(path_value, f"SFX cue {index}", 100)

        if not media_info(path)["has_audio"]:
            raise RuntimeError(f"SFX cue {index} has no audio stream")

    track_status = {}

    for name in ("music", "ambient", "sfx"):
        track = tracks.get(name)
        is_enabled = strict_bool(
            enabled.get(name),
            f"sound_design.enabled.{name}",
            default=(track is not None),
        )

        if not is_enabled:
            track_status[name] = "disabled"
            continue

        if not isinstance(track, dict) or track.get("status") != "completed":
            raise RuntimeError(f"Enabled sound track incomplete: {name}")

        value = track.get("path") or track.get("file") or track.get("output")
        path = safe_output_file(value, f"Sound track {name}", 100)

        if not media_info(path)["has_audio"]:
            raise RuntimeError(f"Sound track has no audio: {name}")

        track_status[name] = "valid"

    return {
        "status": "valid",
        "cue_count": len(cue_rows),
        "tracks": track_status,
        "sfx_enabled": cue_enabled,
        "timing_report": "output/sound_design/sfx_timing_validation.json",
    }


def archive_files():
    files = []
    output_root = OUTPUT.resolve()

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
        if not path.resolve().is_relative_to(output_root):
            raise RuntimeError(f"Refusing path outside output/: {path}")
        files.append(path)

    return sorted(
        files, key=lambda item: item.relative_to(OUTPUT).as_posix()
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
                name = archive_name(path)
                archive.write(path, arcname=name)

                # Hash the bytes actually read back from the ZIP below;
                # the current source hash is also recorded for comparison.
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

            expected_names = {item["path"] for item in entries}
            if set(names) != expected_names:
                raise RuntimeError("ZIP entries do not match manifest")

            for item in entries:
                payload = archive.read(item["path"])
                if len(payload) != item["bytes"]:
                    raise RuntimeError(f"ZIP size mismatch: {item['path']}")

                actual_hash = sha256_bytes(payload)
                if actual_hash != item["sha256"]:
                    raise RuntimeError(f"ZIP hash mismatch: {item['path']}")

        os.replace(temporary, ARCHIVE)
    finally:
        temporary.unlink(missing_ok=True)

    return entries


def reconcile_render_archive(entries, render):
    """Prove all render-manifest deliverables exist unchanged in the ZIP."""
    if not ARCHIVE.is_file() or ARCHIVE.stat().st_size <= 0:
        raise RuntimeError("Archive missing or empty during reconciliation")

    entry_map = {}
    for entry in entries:
        name = entry.get("path")
        if not isinstance(name, str) or not name:
            raise RuntimeError("Archive entry has invalid path")
        if name in entry_map:
            raise RuntimeError(f"Duplicate archive manifest path: {name}")
        entry_map[name] = entry

    required_paths = []

    # The manifest itself must be packaged and verifiable.
    required_paths.append({
        "role": "render_manifest",
        "path": render["render_manifest"],
    })

    for mapping in render["scene_mappings"]:
        required_paths.append({
            "role": "scene_video",
            "part": mapping["part"],
            "local_scene": mapping["local_scene"],
            "global_scene": mapping["global_scene"],
            "path": OUTPUT / mapping["path"],
        })

    for mapping in render["part_mappings"]:
        required_paths.append({
            "role": "part_video",
            "part": mapping["part"],
            "path": OUTPUT / mapping["path"],
        })

    required_paths.extend([
        {"role": "final_video", "path": render["final_video"]},
        {"role": "format_alias", "path": render["alias"]},
    ])

    verified = []
    seen_archive_paths = set()

    with zipfile.ZipFile(ARCHIVE, "r") as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"ZIP integrity failed during reconciliation: {bad}")

        zip_names = archive.namelist()
        if len(zip_names) != len(set(zip_names)):
            raise RuntimeError("Duplicate ZIP entries during reconciliation")

        zip_name_set = set(zip_names)

        for item in required_paths:
            source = Path(item["path"]).resolve()

            if not source.is_file():
                raise RuntimeError(
                    f"Render deliverable disappeared: {source}"
                )

            source_relative = output_relative(source)
            expected_archive_path = "output/" + source_relative

            if expected_archive_path in seen_archive_paths:
                raise RuntimeError(
                    "Two render-manifest entries resolve to the same "
                    f"archive path: {expected_archive_path}"
                )
            seen_archive_paths.add(expected_archive_path)

            if expected_archive_path not in zip_name_set:
                raise RuntimeError(
                    "Render-manifest file missing from ZIP: "
                    f"{expected_archive_path}"
                )

            entry = entry_map.get(expected_archive_path)
            if entry is None:
                raise RuntimeError(
                    "Render file missing from package manifest: "
                    f"{expected_archive_path}"
                )

            payload = archive.read(expected_archive_path)
            zip_bytes = len(payload)
            zip_hash = sha256_bytes(payload)

            # Compare the archived bytes with the current source bytes.
            current_bytes = source.stat().st_size
            current_hash = sha256_file(source)

            if zip_bytes != current_bytes:
                raise RuntimeError(
                    f"ZIP/source size mismatch: {expected_archive_path}"
                )

            if zip_hash != current_hash:
                raise RuntimeError(
                    f"ZIP/source SHA-256 mismatch: {expected_archive_path}"
                )

            # Also compare against the ZIP build manifest.
            if int(entry.get("bytes", -1)) != zip_bytes:
                raise RuntimeError(
                    f"Archive report size mismatch: {expected_archive_path}"
                )

            if entry.get("sha256") != zip_hash:
                raise RuntimeError(
                    f"Archive report hash mismatch: {expected_archive_path}"
                )

            record = {
                "role": item["role"],
                "path": expected_archive_path,
                "bytes": zip_bytes,
                "sha256": zip_hash,
                "verified": True,
            }

            for field in ("part", "local_scene", "global_scene"):
                if field in item:
                    record[field] = item[field]

            verified.append(record)

    expected_scenes = len(render["scene_mappings"])
    expected_parts = len(render["part_mappings"])

    scene_verified = sum(
        item["role"] == "scene_video" for item in verified
    )
    part_verified = sum(
        item["role"] == "part_video" for item in verified
    )

    if scene_verified != expected_scenes:
        raise RuntimeError(
            f"Reconciled {scene_verified}/{expected_scenes} scene videos"
        )

    if part_verified != expected_parts:
        raise RuntimeError(
            f"Reconciled {part_verified}/{expected_parts} part videos"
        )

    roles = {item["role"] for item in verified}
    for role in (
        "render_manifest", "final_video", "format_alias",
    ):
        if role not in roles:
            raise RuntimeError(f"Required reconciliation role missing: {role}")

    return {
        "status": "valid",
        "method": "zip-entry-bytes-and-sha256-compared-with-current-source",
        "manifest_path": archive_name(render["render_manifest"]),
        "expected_scene_count": expected_scenes,
        "scene_entries_verified": scene_verified,
        "expected_part_count": expected_parts,
        "part_entries_verified": part_verified,
        "final_video_entry_verified": True,
        "alias_entry_verified": True,
        "render_manifest_entry_verified": True,
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
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported format: {mode}")
    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")
    if not OUTPUT.is_dir():
        raise RuntimeError("output/ directory does not exist")

    log("PACKAGING:", VERSION)
    log("FORMAT:", mode, "| EXPECTED SCENES:", parts * scenes)

    validation = require_validation(mode, parts, scenes)
    render = validate_render(mode, parts, scenes)
    thumbnail = validate_thumbnail()
    metadata = validate_metadata()
    sound = validate_sound_design(parts, scenes)

    captions = require_file(
        OUTPUT / "captions" / "final_captions.srt",
        "Final captions SRT",
        10,
    )

    if "-->" not in captions.read_text(encoding="utf-8-sig"):
        raise RuntimeError("Final SRT contains no timing cues")

    files = archive_files()
    if not files:
        raise RuntimeError("No output files to package")

    included = {path.resolve() for path in files}
    essential = [
        render["render_manifest"],
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

    # Verify render-manifest paths against actual ZIP entries, not just
    # the list of files that the packager intended to include.
    reconciliation = reconcile_render_archive(entries, render)

    # Verify every packaged file still matches the archive's recorded hash.
    # This also detects source changes that occurred after ZIP creation.
    entry_map = {item["path"]: item for item in entries}
    for path in files:
        name = archive_name(path)
        entry = entry_map.get(name)

        if entry is None:
            raise RuntimeError(f"Packaged file absent from entry list: {name}")

        if not path.is_file():
            raise RuntimeError(f"Packaged source disappeared: {path}")

        if path.stat().st_size != int(entry["bytes"]):
            raise RuntimeError(f"Source changed size after packaging: {name}")

        if sha256_file(path) != entry["sha256"]:
            raise RuntimeError(f"Source changed after packaging: {name}")

    archive_size = ARCHIVE.stat().st_size
    archive_hash = sha256_file(ARCHIVE)

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
            "status": validation["status"],
            "version": validation.get("version"),
        },
        "archive": ARCHIVE.name,
        "archive_bytes": archive_size,
        "archive_sha256": archive_hash,
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
            "part_videos": [
                path.relative_to(ROOT).as_posix()
                for path in render["part_files"]
            ],
            "scene_videos": [
                path.relative_to(ROOT).as_posix()
                for path in render["scene_files"]
            ],
        },
        "sound_design": sound,
        "external_api_calls_during_packaging": 0,
    }

    atomic_json(REPORT, report)

    log("PACKAGING: PASSED")
    log("ARCHIVE:", ARCHIVE.name)
    log("ARCHIVE BYTES:", archive_size)
    log("FILES:", len(entries))
    log("SCENE ZIP RECONCILIATION:", reconciliation["scene_entries_verified"])
    log("PART ZIP RECONCILIATION:", reconciliation["part_entries_verified"])
    log("RENDER MANIFEST ZIP RECONCILIATION: PASSED")
    log("REPORT:", REPORT.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("PACKAGING: FAILED:", str(exc))
        sys.exit(1)
