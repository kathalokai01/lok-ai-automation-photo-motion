
#!/usr/bin/env python3
"""Validate actual Katha Lok AI manifests and final selected-format videos."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes

OUT = ROOT / "output"
PHOTO = OUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO = OUT / "narration" / "audio_jobs.json"
CAPTIONS = OUT / "captions" / "captions.json"
SOUND = OUT / "sound_design" / "sound_design_manifest.json"
SFX = OUT / "sound_design" / "sfx_cues.json"
RENDER = OUT / "scene_renders" / "render_manifest.json"
THUMBNAIL = OUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUT / "thumbnail" / "thumbnail_manifest.json"
METADATA = OUT / "youtube_metadata.txt"
FINAL_SRT = OUT / "captions" / "final_captions.srt"
REPORT = OUT / "validation_report.json"

VERSION = "final-validator-v27-format-aware-manifest-aligned"
# Video dimensions are selected from FORMAT in main(); thumbnails stay landscape.
WIDTH, HEIGHT = 1280, 720
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100


def log(*args):
    print(*args, flush=True)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_report(data):
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temp = REPORT.with_suffix(".json.tmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(REPORT)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path, optional=False):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        if optional and not path.exists():
            return None
        raise RuntimeError(f"Cannot read JSON {path.relative_to(ROOT)}: {exc}") from exc
    return data


def rows(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def pick(row, *keys):
    if not isinstance(row, dict):
        return None
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def safe_file(value, label, minimum=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: file path missing")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: path outside repository")
    if not path.is_file():
        raise RuntimeError(f"{label}: file not found: {path}")
    if path.stat().st_size < minimum:
        raise RuntimeError(f"{label}: file empty or too small: {path.name}")
    return path


def media_info(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=90, check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: {result.stderr[-500:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Invalid media information: {path.name}") from exc

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid duration: {path.name}")

    return {
        "duration": duration,
        "video": video is not None,
        "audio": audio is not None,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
    }


def require_video(path, label, require_audio=True):
    info = media_info(path)
    if not info["video"]:
        raise RuntimeError(f"{label}: video stream missing")
    if require_audio and not info["audio"]:
        raise RuntimeError(f"{label}: audio stream missing")
    if (info["width"], info["height"]) != (WIDTH, HEIGHT):
        raise RuntimeError(
            f"{label}: expected {WIDTH}x{HEIGHT} for selected FORMAT, got "
            f"{info['width']}x{info['height']}"
        )
    return info


def expected_keys(parts, scenes):
    return [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    ]


def ordered_map(items, parts, scenes, label):
    expected = expected_keys(parts, scenes)
    if not isinstance(items, list) or len(items) != len(expected):
        count = len(items) if isinstance(items, list) else 0
        raise RuntimeError(
            f"{label}: expected {len(expected)} scene rows, found {count}"
        )

    result = {}
    for index, row in enumerate(items, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"{label}: invalid row {index}")
        try:
            part = int(row["part"])
            scene = int(row.get("local_scene", row.get("scene", 0)))
            global_scene = int(row.get("global_scene", index))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"{label}: invalid scene mapping at row {index}") from exc

        wanted = expected[index - 1]
        if (part, scene, global_scene) != (*wanted, index):
            raise RuntimeError(
                f"{label}: order mismatch row {index}; "
                f"got {(part, scene, global_scene)}, expected {(*wanted, index)}"
            )

        key = (part, scene)
        if key in result:
            raise RuntimeError(f"{label}: duplicate scene {key}")
        result[key] = row
    return result


def validate_photo(mode, parts, scenes):
    data = read_json(PHOTO)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Photo-motion manifest is not completed")
    if str(data.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Photo-motion FORMAT differs from Input/topic.txt")

    count = parts * scenes
    if int(data.get("expected_scene_count", -1)) != count:
        raise RuntimeError("Photo-motion expected_scene_count mismatch")
    if int(data.get("completed_count", -1)) != count:
        raise RuntimeError("Photo-motion completed_count mismatch")

    mapping = ordered_map(rows(data, "scenes"), parts, scenes, "Photo motion")
    result = {}
    for key, row in mapping.items():
        if row.get("status") != "completed":
            raise RuntimeError(f"Photo-motion scene incomplete: {key}")

        path = safe_file(
            pick(row, "video", "source_video", "output", "path"),
            f"Photo-motion scene {key}", MIN_VIDEO_BYTES,
        )
        info = require_video(path, f"Photo-motion scene {key}", require_audio=False)
        actual_hash = sha256(path)
        declared_hash = row.get("video_sha256")
        if declared_hash and declared_hash != actual_hash:
            raise RuntimeError(f"Photo-motion hash mismatch: {key}")

        result[key] = {
            "path": path,
            "sha256": actual_hash,
            "duration": info["duration"],
        }

    return result


def validate_audio(parts, scenes):
    data = read_json(AUDIO)
    jobs = rows(data, "jobs", "audio_jobs", "scenes")
    if not jobs:
        raise RuntimeError("Narration manifest contains no audio jobs")

    mapping = {}
    for index, row in enumerate(jobs, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid narration row {index}")
        try:
            key = (int(row["part"]), int(row.get("scene", row.get("local_scene", 0))))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid narration mapping row {index}") from exc

        if key in mapping:
            raise RuntimeError(f"Duplicate narration scene: {key}")
        if row.get("status") != "completed":
            raise RuntimeError(f"Narration is not completed: {key}")

        path = safe_file(
            pick(row, "output", "audio_path", "path", "file", "audio"),
            f"Narration {key}", MIN_AUDIO_BYTES,
        )
        info = media_info(path)
        if not info["audio"]:
            raise RuntimeError(f"Narration has no audio stream: {key}")

        mapping[key] = {
            "path": path,
            "sha256": sha256(path),
            "duration": info["duration"],
        }

    expected = set(expected_keys(parts, scenes))
    if set(mapping) != expected:
        raise RuntimeError(
            f"Narration mapping mismatch; missing={sorted(expected-set(mapping))}; "
            f"extra={sorted(set(mapping)-expected)}"
        )
    return mapping


def validate_captions(mode, parts, scenes):
    data = read_json(CAPTIONS)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Caption manifest is not completed")
    if str(data.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Caption FORMAT differs from Input/topic.txt")

    mapping = {}
    for index, row in enumerate(rows(data, "captions"), 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Invalid caption row {index}")
        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid caption mapping row {index}") from exc

        if key in mapping:
            raise RuntimeError(f"Duplicate caption scene: {key}")
        if row.get("status") not in (None, "completed"):
            raise RuntimeError(f"Caption scene incomplete: {key}")
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"Caption text empty: {key}")
        mapping[key] = text.strip()

    expected = set(expected_keys(parts, scenes))
    if set(mapping) != expected:
        raise RuntimeError(
            f"Caption mapping mismatch; missing={sorted(expected-set(mapping))}; "
            f"extra={sorted(set(mapping)-expected)}"
        )

    srt = safe_file(str(FINAL_SRT.relative_to(ROOT)), "Final SRT", 10)
    srt_text = srt.read_text(encoding="utf-8-sig")
    if "-->" not in srt_text:
        raise RuntimeError("Final SRT contains no timing cues")

    return {
        "status": "valid",
        "scene_count": len(mapping),
        "final_srt": srt.relative_to(ROOT).as_posix(),
        "final_srt_sha256": sha256(srt),
        "timing_note": "Phrase-level proportional timing; not forced alignment",
    }


def validate_sound():
    data = read_json(SOUND)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Sound-design manifest is not completed")

    tracks = data.get("tracks")
    if not isinstance(tracks, dict):
        raise RuntimeError("Sound-design tracks object missing")

    checked = {}
    for name, record in tracks.items():
        if record is None:
            checked[name] = {"status": "disabled"}
            continue
        if not isinstance(record, dict) or record.get("status") != "completed":
            raise RuntimeError(f"Sound track incomplete: {name}")

        path = safe_file(
            pick(record, "path", "file", "output", "audio_path"),
            f"Sound track {name}", MIN_AUDIO_BYTES,
        )
        info = media_info(path)
        if not info["audio"]:
            raise RuntimeError(f"Sound track has no audio: {name}")
        actual_hash = sha256(path)
        if record.get("sha256") and record["sha256"] != actual_hash:
            raise RuntimeError(f"Sound track hash mismatch: {name}")
        checked[name] = {
            "status": "valid",
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": actual_hash,
        }

    cue_data = read_json(SFX, optional=True)
    cue_count = 0
    if cue_data is not None:
        if not isinstance(cue_data, dict) or cue_data.get("status") != "completed":
            raise RuntimeError("SFX cue manifest is not completed")
        for index, cue in enumerate(rows(cue_data, "cues", "items"), 1):
            if not isinstance(cue, dict):
                raise RuntimeError(f"Invalid SFX cue row {index}")
            try:
                part = int(cue["part"])
                scene = int(cue["scene"])
                time = float(cue.get("time", 0))
                gain = float(cue.get("gain", 0))
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid SFX cue mapping row {index}") from exc
            if part < 1 or scene < 1 or not math.isfinite(time) or time < 0:
                raise RuntimeError(f"Invalid SFX cue timing row {index}")
            if not math.isfinite(gain) or gain < 0:
                raise RuntimeError(f"Invalid SFX cue gain row {index}")
            path = safe_file(
                pick(cue, "file", "path", "audio", "output"),
                f"SFX cue {index}", MIN_AUDIO_BYTES,
            )
            if not media_info(path)["audio"]:
                raise RuntimeError(f"SFX cue has no audio: {index}")
            cue_count += 1

    return {
        "status": "valid",
        "tracks": checked,
        "sfx_cue_count": cue_count,
        "external_api_calls": data.get("external_api_calls", 0),
    }


def validate_render(mode, parts, scenes):
    data = read_json(RENDER)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Final render manifest is not completed")
    if str(data.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Final render FORMAT mismatch")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Final render manifest does not confirm burned captions")

    count = parts * scenes
    if int(data.get("completed_count", -1)) != count:
        raise RuntimeError("Render completed_count mismatch")

    mapping = ordered_map(
        rows(data, "completed_scenes", "scenes"), parts, scenes, "Final render"
    )
    scene_results = []
    for key, row in mapping.items():
        if row.get("status") != "completed" or row.get("captions_burned") is not True:
            raise RuntimeError(f"Captioned scene incomplete: {key}")

        path = safe_file(
            pick(row, "video", "output", "rendered_video"),
            f"Captioned scene {key}", MIN_VIDEO_BYTES,
        )
        info = require_video(path, f"Captioned scene {key}")
        declared = row.get("duration")
        if declared is not None and abs(float(declared) - info["duration"]) > 1:
            raise RuntimeError(f"Scene duration mismatch: {key}")

        actual_hash = sha256(path)
        declared_hash = pick(row, "sha256", "video_sha256")
        if declared_hash and declared_hash != actual_hash:
            raise RuntimeError(f"Captioned scene hash mismatch: {key}")

        scene_results.append({
            "part": key[0], "scene": key[1],
            "path": path.relative_to(ROOT).as_posix(),
            "duration": round(info["duration"], 3),
            "sha256": actual_hash,
        })

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        raise RuntimeError(f"Expected exactly {parts} part videos")

    part_results = []
    for index, row in enumerate(part_rows, 1):
        if isinstance(row, dict):
            if int(row.get("part", index)) != index:
                raise RuntimeError(f"Part order mismatch: {index}")
            if row.get("status") not in (None, "completed"):
                raise RuntimeError(f"Part incomplete: {index}")
            value = pick(row, "path", "video", "output")
        else:
            value = row

        path = safe_file(value, f"Part video {index}", MIN_VIDEO_BYTES)
        info = require_video(path, f"Part video {index}")
        part_results.append({
            "part": index,
            "path": path.relative_to(ROOT).as_posix(),
            "duration": round(info["duration"], 3),
            "sha256": sha256(path),
        })

    final = safe_file(data.get("final_video"), "Final video", MIN_VIDEO_BYTES)
    alias = safe_file(data.get("alias"), "Selected-format alias", MIN_VIDEO_BYTES)
    final_info = require_video(final, "Final video")
    alias_info = require_video(alias, "Selected-format alias")

    expected_alias = (
        "katha_lok_ai_short.mp4" if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias.name != expected_alias:
        raise RuntimeError(f"Expected alias {expected_alias}, got {alias.name}")
    if final.resolve() == alias.resolve():
        raise RuntimeError("Final video and alias must be separate files")
    if abs(final_info["duration"] - alias_info["duration"]) > 1:
        raise RuntimeError("Final video and alias durations differ")

    part_total = sum(row["duration"] for row in part_results)
    scene_total = sum(row["duration"] for row in scene_results)
    if abs(final_info["duration"] - part_total) > max(2, parts):
        raise RuntimeError("Final duration does not match total part duration")
    if abs(final_info["duration"] - scene_total) > max(2, count):
        raise RuntimeError("Final duration does not match total scene duration")

    opposite = (
        "katha_lok_ai_full.mp4" if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    for candidate in (OUT / "videos" / opposite, OUT / opposite):
        if candidate.exists():
            raise RuntimeError(f"Stale opposite-format video exists: {candidate}")

    return {
        "status": "valid",
        "scene_count": len(scene_results),
        "scenes": scene_results,
        "parts": part_results,
        "final_video": final.relative_to(ROOT).as_posix(),
        "final_video_sha256": sha256(final),
        "alias": alias.relative_to(ROOT).as_posix(),
        "alias_sha256": sha256(alias),
        "final_duration_seconds": round(final_info["duration"], 3),
        "resolution": [WIDTH, HEIGHT],
        "captions_burned": True,
    }


def validate_thumbnail(mode):
    path = safe_file("output/thumbnail.jpg", "Thumbnail", MIN_VIDEO_BYTES)
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            dimensions, image_format = image.size, image.format
    except Exception as exc:
        raise RuntimeError(f"Invalid thumbnail: {exc}") from exc

    # YouTube thumbnail remains 16:9 in both short and full video modes.
    if dimensions != (1280, 720) or image_format != "JPEG":
        raise RuntimeError(
            f"Thumbnail must be 1280x720 JPEG; got {image_format} {dimensions}"
        )

    manifest = read_json(THUMBNAIL_MANIFEST, optional=True)
    if manifest is not None:
        if not isinstance(manifest, dict) or manifest.get("status") != "completed":
            raise RuntimeError("Thumbnail manifest is not completed")
        if str(manifest.get("format", "")).lower() != mode:
            raise RuntimeError("Thumbnail manifest FORMAT mismatch")
        declared = pick(manifest, "thumbnail", "path", "output")
        if declared and safe_file(declared, "Thumbnail manifest").resolve() != path.resolve():
            raise RuntimeError("Thumbnail manifest points to a different file")

    return {
        "status": "valid",
        "path": path.relative_to(ROOT).as_posix(),
        "sha256": sha256(path),
        "resolution": list(dimensions),
    }


def validate_metadata():
    path = safe_file("output/youtube_metadata.txt", "YouTube metadata", 10)
    content = path.read_text(encoding="utf-8-sig").strip()
    sections = {}
    current = None

    for line in content.splitlines():
        normalized = line.strip().strip("[]").rstrip(":").strip().upper()
        if normalized in {"TITLE", "DESCRIPTION", "TAGS", "LANGUAGE"}:
            current = normalized
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line.strip())

    title = " ".join(sections.get("TITLE", [])).strip()
    description = " ".join(sections.get("DESCRIPTION", [])).strip()
    tags = " ".join(sections.get("TAGS", [])).strip()

    if not title or not description or not tags:
        raise RuntimeError(
            "Metadata must contain non-empty TITLE, DESCRIPTION and TAGS sections"
        )
    if len(title) > 100:
        raise RuntimeError(f"Metadata title is too long: {len(title)} characters")

    return {
        "status": "valid",
        "path": path.relative_to(ROOT).as_posix(),
        "sha256": sha256(path),
        "title_length": len(title),
        "description_length": len(description),
        "tags_length": len(tags),
    }


def main():
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(f"{command} is required")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = str(get_format(config)).strip().lower()
    parts, scenes = int(get_parts(config)), int(get_scenes(config))

    if mode not in ("short", "full"):
        raise RuntimeError(f"Unsupported FORMAT: {mode}")

    # Shorts are portrait 9:16; full videos remain landscape 16:9.
    global WIDTH, HEIGHT
    if mode == "short":
        WIDTH, HEIGHT = 720, 1280
    else:
        WIDTH, HEIGHT = 1280, 720

    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")
    if not OUT.is_dir():
        raise RuntimeError("output/ directory is missing")

    log("VALIDATION VERSION:", VERSION)
    log("FORMAT:", mode, "| PARTS:", parts, "| SCENES/PART:", scenes)

    checks = {}
    photo = validate_photo(mode, parts, scenes)
    checks["photo_motion"] = {
        "status": "valid", "scene_count": len(photo),
        "verified_hashes": sum(bool(item["sha256"]) for item in photo.values()),
    }

    audio = validate_audio(parts, scenes)
    checks["narration"] = {
        "status": "valid", "scene_count": len(audio),
        "verified_hashes": sum(bool(item["sha256"]) for item in audio.values()),
    }

    checks["captions"] = validate_captions(mode, parts, scenes)
    checks["sound_design"] = validate_sound()
    checks["render"] = validate_render(mode, parts, scenes)
    checks["thumbnail"] = validate_thumbnail(mode)
    checks["metadata"] = validate_metadata()

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": parts,
        "scenes_per_part": scenes,
        "expected_scenes": parts * scenes,
        "video_dimensions": [WIDTH, HEIGHT],
        "checks": checks,
        "render_details": checks["render"],
        "limitations": [
            "Caption timing is phrase-level proportional estimation, not forced alignment.",
            "Manifest validation confirms declared outputs and media streams, not subjective story quality.",
        ],
    }
    write_report(report)
    log("VALIDATION: PASSED")
    log("FORMAT:", mode)
    log("SCENES:", parts * scenes)
    log("REPORT:", REPORT.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("VALIDATION: FAILED:", str(exc))
        try:
            write_report({
                "status": "failed",
                "version": VERSION,
                "error": str(exc),
            })
        except Exception:
            pass
        sys.exit(1)
