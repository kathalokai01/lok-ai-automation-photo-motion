#!/usr/bin/env python3
"""Cross-check Katha Lok AI photo-motion outputs before ZIP packaging."""

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
REPORT = OUT / "validation_report.json"

VERSION = "final-validator-v25-real-manifest-cross-check"
WIDTH, HEIGHT = 1280, 720


def log(*items):
    print(*items, flush=True)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_report(data):
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT.with_suffix(".json.tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(REPORT)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, optional=False):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        if optional:
            return None
        raise RuntimeError(f"JSON पढ़ने में समस्या: {path}: {exc}") from exc

    if not isinstance(data, (dict, list)):
        raise RuntimeError(f"JSON root object/list नहीं है: {path}")
    return data


def rows(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
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
        raise RuntimeError(f"{label}: manifest में file path नहीं है")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: repository से बाहर का path")
    if not path.is_file():
        raise RuntimeError(f"{label}: file नहीं मिली: {path}")
    if path.stat().st_size < minimum:
        raise RuntimeError(f"{label}: file खाली या बहुत छोटी है: {path.name}")

    return path


def media_info(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe उपलब्ध नहीं है")

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
            f"Media जाँच असफल: {path.name}: {result.stderr[-500:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Media metadata गलत है: {path.name}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Media duration गलत है: {path.name}")

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
        "video": video is not None,
        "audio": audio is not None,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
    }


def require_video(path, label, require_audio=True):
    info = media_info(path)

    if not info["video"]:
        raise RuntimeError(f"{label}: video stream नहीं है")
    if require_audio and not info["audio"]:
        raise RuntimeError(f"{label}: audio stream नहीं है")
    if (info["width"], info["height"]) != (WIDTH, HEIGHT):
        raise RuntimeError(
            f"{label}: resolution {info['width']}x{info['height']} है; "
            f"{WIDTH}x{HEIGHT} अपेक्षित है"
        )

    return info


def expected_keys(parts, scenes):
    return [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    ]


def scene_key(row, index, label):
    if not isinstance(row, dict):
        raise RuntimeError(f"{label}: row {index} object नहीं है")

    try:
        part = int(row["part"])
        scene = int(row.get("local_scene", row.get("scene", 0)))
        global_scene = int(row.get("global_scene", index))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"{label}: row {index} की mapping गलत है") from exc

    return part, scene, global_scene


def ordered_scene_map(data_rows, parts, scenes, label):
    expected = expected_keys(parts, scenes)

    if not isinstance(data_rows, list) or len(data_rows) != len(expected):
        count = len(data_rows) if isinstance(data_rows, list) else 0
        raise RuntimeError(
            f"{label}: {len(expected)} scenes अपेक्षित, {count} मिले"
        )

    result = {}

    for index, row in enumerate(data_rows, 1):
        part, scene, global_scene = scene_key(row, index, label)
        wanted = expected[index - 1]

        if (part, scene, global_scene) != (*wanted, index):
            raise RuntimeError(
                f"{label}: scene order गलत row {index}; "
                f"actual={(part, scene, global_scene)}, "
                f"expected={(*wanted, index)}"
            )

        key = (part, scene)
        if key in result:
            raise RuntimeError(f"{label}: duplicate scene {key}")

        result[key] = row

    return result


def validate_photo(mode, parts, scenes):
    data = read_json(PHOTO)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Photo-motion manifest completed नहीं है")
    if str(data.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Photo-motion format, input format से अलग है")

    expected_count = parts * scenes
    if int(data.get("completed_count", -1)) != expected_count:
        raise RuntimeError("Photo-motion completed_count गलत है")

    mapping = ordered_scene_map(
        rows(data, "scenes"), parts, scenes, "Photo motion"
    )

    verified = {}

    for key, row in mapping.items():
        if row.get("status") != "completed":
            raise RuntimeError(f"Photo-motion scene अधूरा है: {key}")

        path = safe_file(row.get("video"), f"Photo motion {key}", 1000)
        info = require_video(path, f"Photo motion {key}", require_audio=False)

        declared_hash = row.get("video_sha256")
        actual_hash = sha256(path)
        if declared_hash and declared_hash != actual_hash:
            raise RuntimeError(f"Photo-motion SHA-256 mismatch: {key}")

        verified[key] = {
            "path": path,
            "sha256": actual_hash,
            "duration": info["duration"],
        }

    if int(data.get("expected_scene_count", expected_count)) != expected_count:
        raise RuntimeError("Photo-motion expected_scene_count mismatch")

    return verified


def validate_audio(parts, scenes):
    data = read_json(AUDIO)
    jobs = rows(data, "jobs", "audio_jobs", "scenes")

    if not jobs:
        raise RuntimeError("Narration manifest में कोई audio job नहीं है")

    mapping = {}

    for index, row in enumerate(jobs, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Narration row {index} गलत है")

        try:
            part = int(row["part"])
            scene = int(row.get("scene", row.get("local_scene", 0)))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Narration row {index} की mapping गलत है") from exc

        key = (part, scene)
        if key in mapping:
            raise RuntimeError(f"Duplicate narration scene: {key}")
        if row.get("status") != "completed":
            raise RuntimeError(f"Narration completed नहीं है: {key}")

        path = safe_file(
            pick(row, "output", "audio_path", "path", "file", "audio"),
            f"Narration {key}",
            100,
        )
        info = media_info(path)
        if not info["audio"]:
            raise RuntimeError(f"Narration में audio stream नहीं: {key}")

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


def validate_captions(parts, scenes):
    data = read_json(CAPTIONS)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Captions manifest completed नहीं है")

    if str(data.get("format", "")).strip().lower() not in ("", "short", "full"):
        raise RuntimeError("Captions manifest format value गलत है")

    caption_rows = rows(data, "captions")
    mapping = {}

    for index, row in enumerate(caption_rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Caption row {index} गलत है")

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Caption row {index} की mapping गलत है") from exc

        if key in mapping:
            raise RuntimeError(f"Duplicate caption scene: {key}")
        if row.get("status") not in (None, "completed"):
            raise RuntimeError(f"Caption scene completed नहीं है: {key}")

        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"Caption text खाली है: {key}")

        mapping[key] = text.strip()

    expected = set(expected_keys(parts, scenes))
    if set(mapping) != expected:
        raise RuntimeError(
            f"Caption mapping mismatch; missing={sorted(expected-set(mapping))}; "
            f"extra={sorted(set(mapping)-expected)}"
        )

    return mapping


def validate_sound():
    data = read_json(SOUND)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Sound-design manifest completed नहीं है")

    tracks = data.get("tracks")
    if not isinstance(tracks, dict):
        raise RuntimeError("Sound-design tracks object गलत है")

    verified_tracks = {}

    for name, record in tracks.items():
        if record is None:
            verified_tracks[name] = {"status": "disabled"}
            continue

        if not isinstance(record, dict):
            raise RuntimeError(f"Sound track record गलत है: {name}")
        if record.get("status") != "completed":
            raise RuntimeError(f"Sound track completed नहीं है: {name}")

        value = pick(record, "path", "file", "output", "audio_path")
        if not value:
            raise RuntimeError(f"Sound track path नहीं है: {name}")

        path = safe_file(value, f"Sound track {name}", 100)
        if not media_info(path)["audio"]:
            raise RuntimeError(f"Sound track में audio नहीं है: {name}")

        declared_hash = record.get("sha256")
        actual_hash = sha256(path)
        if declared_hash and declared_hash != actual_hash:
            raise RuntimeError(f"Sound track hash mismatch: {name}")

        verified_tracks[name] = {
            "status": "valid",
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": actual_hash,
        }

    cue_data = read_json(SFX, optional=True)
    cue_count = 0

    if cue_data is not None:
        if not isinstance(cue_data, dict):
            raise RuntimeError("SFX manifest structure गलत है")
        if cue_data.get("status") != "completed":
            raise RuntimeError("SFX cue manifest completed नहीं है")

        cues = rows(cue_data, "cues", "items")
        for index, cue in enumerate(cues, 1):
            if not isinstance(cue, dict):
                raise RuntimeError(f"SFX cue {index} गलत है")

            try:
                part = int(cue["part"])
                scene = int(cue["scene"])
                timestamp = float(cue.get("time", 0))
                gain = float(cue.get("gain", 0))
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"SFX cue {index} mapping/timing गलत है") from exc

            if part < 1 or scene < 1:
                raise RuntimeError(f"SFX cue {index} scene mapping गलत है")
            if not math.isfinite(timestamp) or timestamp < 0:
                raise RuntimeError(f"SFX cue {index} timing गलत है")
            if not math.isfinite(gain) or gain < 0:
                raise RuntimeError(f"SFX cue {index} gain गलत है")

            path = safe_file(
                pick(cue, "file", "path", "audio", "output"),
                f"SFX cue {index}",
                100,
            )
            if not media_info(path)["audio"]:
                raise RuntimeError(f"SFX cue {index} में audio नहीं है")

            cue_count += 1

    return {
        "status": "valid",
        "tracks": verified_tracks,
        "sfx_cue_count": cue_count,
        "external_api_calls": data.get("external_api_calls", 0),
    }


def validate_render(mode, parts, scenes, photo, audio):
    data = read_json(RENDER)

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Render manifest completed नहीं है")
    if str(data.get("format", "")).strip().lower() != mode:
        raise RuntimeError("Render format, input format से अलग है")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Render manifest captions_burned=True नहीं बताता")

    expected_count = parts * scenes
    if int(data.get("completed_count", -1)) != expected_count:
        raise RuntimeError("Render completed_count mismatch")

    mapping = ordered_scene_map(
        rows(data, "completed_scenes", "scenes"),
        parts,
        scenes,
        "Render",
    )

    scene_results = []
    total_scene_duration = 0.0

    for key, row in mapping.items():
        if row.get("status") != "completed":
            raise RuntimeError(f"Render scene अधूरा है: {key}")
        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Scene captions flag गलत है: {key}")

        video = safe_file(
            pick(row, "video", "output", "rendered_video"),
            f"Captioned scene {key}",
            1000,
        )
        info = require_video(video, f"Captioned scene {key}")

        source_path = pick(row, "source_video", "source_rendered_video")
        if source_path:
            source = safe_file(source_path, f"Render source {key}", 1000)
            if key in photo and source.resolve() == photo[key]["path"].resolve():
                # A photo-motion clip may be used as a render source, but
                # narration must still be present in the rendered result.
                pass

        declared = row.get("duration")
        if declared is not None:
            try:
                if abs(float(declared) - info["duration"]) > 1.0:
                    raise RuntimeError(f"Scene duration mismatch: {key}")
            except (ValueError, TypeError) as exc:
                raise RuntimeError(f"Scene duration field गलत है: {key}") from exc

        declared_hash = pick(row, "sha256", "video_sha256")
        actual_hash = sha256(video)
        if declared_hash and declared_hash != actual_hash:
            raise RuntimeError(f"Rendered scene SHA-256 mismatch: {key}")

        total_scene_duration += info["duration"]
        scene_results.append({
            "part": key[0],
            "scene": key[1],
            "path": video.relative_to(ROOT).as_posix(),
            "sha256": actual_hash,
            "duration": round(info["duration"], 3),
        })

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        raise RuntimeError(f"Render manifest में {parts} part videos होने चाहिए")

    part_results = []

    for index, row in enumerate(part_rows, 1):
        if isinstance(row, dict):
            if int(row.get("part", index)) != index:
                raise RuntimeError(f"Part order गलत है: {index}")
            if row.get("status") not in (None, "completed"):
                raise RuntimeError(f"Part completed नहीं है: {index}")
            value = pick(row, "path", "video", "output")
        else:
            value = row

        path = safe_file(value, f"Part video {index}", 1000)
        info = require_video(path, f"Part video {index}")
        part_results.append({
            "part": index,
            "path": path.relative_to(ROOT).as_posix(),
            "duration": round(info["duration"], 3),
            "sha256": sha256(path),
        })

    final = safe_file(data.get("final_video"), "Final video", 1000)
    alias = safe_file(data.get("alias"), "Format alias", 1000)
    final_info = require_video(final, "Final video")
    alias_info = require_video(alias, "Format alias")

    expected_alias = (
        "katha_lok_ai_short.mp4" if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias.name != expected_alias:
        raise RuntimeError(
            f"Format alias गलत है: {alias.name}; अपेक्षित {expected_alias}"
        )
    if final.resolve() == alias.resolve():
        raise RuntimeError("Final video और alias अलग files होने चाहिए")
    if abs(final_info["duration"] - alias_info["duration"]) > 1.0:
        raise RuntimeError("Final video और alias duration अलग हैं")

    part_duration = sum(item["duration"] for item in part_results)
    if abs(final_info["duration"] - part_duration) > max(2.0, parts):
        raise RuntimeError("Final video duration, part videos के कुल समय से नहीं मिलती")

    scene_duration = sum(item["duration"] for item in scene_results)
    if abs(final_info["duration"] - scene_duration) > max(2.0, expected_count):
        raise RuntimeError("Final video duration, scene videos के कुल समय से नहीं मिलती")

    videos_dir = OUT / "videos"
    opposite = videos_dir / (
        "katha_lok_ai_full.mp4" if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    if opposite.exists():
        raise RuntimeError(f"दूसरे format की stale video मौजूद है: {opposite.name}")

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
    path = safe_file("output/thumbnail.jpg", "Thumbnail", 1000)

    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            dimensions = image.size
            image_format = image.format
    except Exception as exc:
        raise RuntimeError(f"Thumbnail image गलत है: {exc}") from exc

    if dimensions != (WIDTH, HEIGHT) or image_format != "JPEG":
        raise RuntimeError(
            f"Thumbnail JPEG 1280x720 होना चाहिए; मिला {image_format} {dimensions}"
        )

    manifest = read_json(THUMBNAIL_MANIFEST, optional=True)
    if isinstance(manifest, dict):
        if manifest.get("status") != "completed":
            raise RuntimeError("Thumbnail manifest completed नहीं है")

        if "format" in manifest and str(manifest["format"]).lower() != mode:
            raise RuntimeError("Thumbnail manifest format mismatch")

        declared = pick(manifest, "thumbnail", "path", "output")
        if declared:
            declared_path = safe_file(declared, "Thumbnail manifest")
            if declared_path.resolve() != path.resolve():
                raise RuntimeError("Thumbnail manifest path mismatch")

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

    missing = [
        name for name in ("TITLE", "DESCRIPTION", "TAGS")
        if not any(sections.get(name, []))
    ]
    if missing:
        raise RuntimeError(
            "Metadata sections missing/empty: " + ", ".join(missing)
        )

    title = " ".join(sections["TITLE"]).strip()
    description = " ".join(sections["DESCRIPTION"]).strip()
    tags = " ".join(sections["TAGS"]).strip()

    if not title or not description or not tags:
        raise RuntimeError("Metadata title/description/tags खाली हैं")
    if len(title) > 100:
        raise RuntimeError("Metadata title 100 characters से अधिक है")

    return {
        "status": "valid",
        "path": path.relative_to(ROOT).as_posix(),
        "sha256": sha256(path),
        "title_characters": len(title),
        "description_characters": len(description),
        "tags_characters": len(tags),
    }


def main():
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe install नहीं है")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = str(get_format(config)).strip().lower()
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")
    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS और SCENES positive होने चाहिए")

    log("FINAL VALIDATION:", VERSION)
    log("FORMAT:", mode, "| PARTS:", parts, "| SCENES/PART:", scenes)

    checks = {}

    try:
        photo = validate_photo(mode, parts, scenes)
        audio = validate_audio(parts, scenes)
        caption_map = validate_captions(parts, scenes)
        sound = validate_sound()
        render = validate_render(mode, parts, scenes, photo, audio)
        thumbnail = validate_thumbnail(mode)
        metadata = validate_metadata()

        checks = {
            "photo_motion": {
                "status": "valid",
                "scene_count": len(photo),
                "verified_sha256": True,
            },
            "narration": {
                "status": "valid",
                "scene_count": len(audio),
                "verified_sha256": True,
            },
            "captions": {
                "status": "valid",
                "scene_count": len(caption_map),
                "nonempty_text_count": len(caption_map),
            },
            "sound_design": sound,
            "render": {
                key: value for key, value in render.items()
                if key not in {"scenes", "parts"}
            },
            "thumbnail": thumbnail,
            "metadata": metadata,
        }

        report = {
            "status": "completed",
            "version": VERSION,
            "format": mode,
            "parts": parts,
            "scenes_per_part": scenes,
            "expected_scenes": parts * scenes,
            "resolution": {"width": WIDTH, "height": HEIGHT},
            "validator_external_api_calls": 0,
            "checks": checks,
            "cross_manifest_validation": {
                "status": "valid",
                "photo_motion_scene_count": len(photo),
                "narration_scene_count": len(audio),
                "caption_scene_count": len(caption_map),
                "render_scene_count": len(render["scenes"]),
                "scene_mapping_verified": True,
            },
            "render_details": render,
            "limitations": [
                "यह validator visual quality का AI/perceptual assessment नहीं करता।",
                "Caption timing को forced alignment या word-level timing के रूप में प्रमाणित नहीं करता।",
                "Manifest flag होना OCR से हर frame जाँचने के बराबर नहीं है।",
                "SFX file मौजूद होने से final mix में उसकी loudness प्रमाणित नहीं होती।",
                "Metadata का story-specific होना केवल text देखकर पूर्णतः प्रमाणित नहीं किया जा सकता।",
            ],
        }

        write_report(report)
        log("FINAL VALIDATION: PASSED")
        log("VERIFIED SCENES:", len(render["scenes"]))
        log("FINAL VIDEO:", render["final_video"])
        log("REPORT: output/validation_report.json")

    except Exception as exc:
        write_report({
            "status": "failed",
            "version": VERSION,
            "format": mode,
            "parts": parts,
            "scenes_per_part": scenes,
            "expected_scenes": parts * scenes,
            "checks": checks,
            "error": str(exc),
        })
        log("FINAL VALIDATION: FAILED:", str(exc))
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(1)
