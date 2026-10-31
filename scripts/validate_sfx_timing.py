#!/usr/bin/env python3
"""Strict SFX validation against actual narration and renderer requirements."""

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

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes,
)

INPUT_FILE = ROOT / "Input" / "topic.txt"
AUDIO_FILE = ROOT / "output" / "narration" / "audio_jobs.json"
SOUND_MANIFEST = ROOT / "output" / "sound_design" / "sound_design_manifest.json"
CUES_FILE = ROOT / "output" / "sound_design" / "sfx_cues.json"
OUTPUT_FILE = ROOT / "output" / "sound_design" / "sfx_timing_validation.json"

VERSION = "sfx-validator-v9-renderer-compatible-dynamic-parts"
MIN_AUDIO_BYTES = 100
EPSILON = 0.001
MAX_GAIN = 1.0
MAX_CUES_PER_SCENE = 12

TIME_KEYS = (
    "time", "time_seconds", "timestamp",
    "start_seconds", "start",
)
PATH_KEYS = (
    "file", "path", "audio", "output", "audio_path",
)
SFX_KEYS = (
    "SFX_ENABLED", "SFX", "SOUND_EFFECTS",
    "SOUND_EFFECTS_ENABLED",
)


def log(*values):
    print(*values, flush=True)


def read_json(path):
    try:
        value = json.loads(
            path.read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot read JSON: {path}: {exc}"
        ) from exc

    if not isinstance(value, (dict, list)):
        raise RuntimeError(
            f"Invalid JSON structure: {path}"
        )

    return value


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    try:
        temporary.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def rows_from(data, *keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def first_value(row, keys):
    if not isinstance(row, dict):
        return None

    for key in keys:
        value = row.get(key)
        if value is not None:
            return value

    return None


def config_bool(config, keys, default=True):
    for key in keys:
        if key not in config:
            continue

        value = config[key]

        if isinstance(value, bool):
            return value

        value = str(value).strip().lower()

        if value in {
            "true", "yes", "1", "on", "enabled",
        }:
            return True

        if value in {
            "false", "no", "0", "off", "disabled",
        }:
            return False

        raise RuntimeError(
            f"{key} must be true or false"
        )

    return default


def positive_int(value, label):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{label} must be an integer"
        ) from exc

    if result < 1:
        raise RuntimeError(
            f"{label} must be at least 1"
        )

    return result


def required_number(
    row,
    keys,
    label,
    minimum=None,
    maximum=None,
):
    value = first_value(row, keys)

    if value is None or (
        isinstance(value, str) and not value.strip()
    ):
        raise RuntimeError(
            f"{label}: required numeric field is missing"
        )

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{label}: invalid number {value!r}"
        ) from exc

    if not math.isfinite(number):
        raise RuntimeError(
            f"{label}: number must be finite"
        )

    if minimum is not None and number < minimum:
        raise RuntimeError(
            f"{label}: value must be >= {minimum}"
        )

    if maximum is not None and number > maximum:
        raise RuntimeError(
            f"{label}: value must be <= {maximum}"
        )

    return number


def safe_file(value, label, minimum_bytes=1):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(
            f"{label}: file path is missing"
        )

    path = Path(value.strip())

    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(
            f"{label}: path escapes project directory"
        )

    if not path.is_file():
        raise RuntimeError(
            f"{label}: file not found: {path}"
        )

    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(
            f"{label}: file is too small: {path}"
        )

    return path


def relative(path):
    return path.resolve().relative_to(
        ROOT.resolve()
    ).as_posix()


def sha256_file(path):
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            digest.update(block)

    return digest.hexdigest()


def media_info(path):
    executable = shutil.which("ffprobe")

    if not executable:
        raise RuntimeError(
            "ffprobe is required but not installed"
        )

    result = subprocess.run(
        [
            executable,
            "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type",
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
            f"ffprobe failed for {path.name}: "
            f"{result.stderr[-1000:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"Invalid media metadata: {path.name}"
        ) from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(
            f"Invalid media duration: {path.name}"
        )

    has_audio = any(
        isinstance(stream, dict)
        and stream.get("codec_type") == "audio"
        for stream in data.get("streams", [])
    )

    if not has_audio:
        raise RuntimeError(
            f"Audio stream missing: {path.name}"
        )

    return {
        "duration": duration,
        "bytes": path.stat().st_size,
    }


def expected_keys(parts, scenes):
    return {
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    }


def resolve_actual_parts(
    data,
    jobs,
    mode,
    configured_parts,
    scenes,
):
    if not isinstance(data, dict):
        raise RuntimeError(
            "Narration manifest must be a JSON object"
        )

    if data.get("status") != "completed":
        raise RuntimeError(
            "Narration manifest is not completed"
        )

    manifest_format = str(
        data.get("format", "")
    ).strip().lower()

    if manifest_format and manifest_format != mode:
        raise RuntimeError(
            f"Narration FORMAT mismatch: "
            f"Input={mode}, manifest={manifest_format}"
        )

    discovered = set()

    for index, row in enumerate(jobs, 1):
        if not isinstance(row, dict):
            raise RuntimeError(
                f"Invalid narration row {index}"
            )

        try:
            part = int(row["part"])
            scene = int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid narration mapping at row {index}"
            ) from exc

        if part < 1 or scene < 1 or scene > scenes:
            raise RuntimeError(
                f"Invalid narration mapping: {part}/{scene}"
            )

        discovered.add(part)

    if not discovered:
        raise RuntimeError(
            "Narration manifest contains no part numbers"
        )

    actual_parts = max(discovered)

    if discovered != set(range(1, actual_parts + 1)):
        raise RuntimeError(
            "Narration part numbering is not continuous"
        )

    declared = data.get("total_parts")

    if declared is not None:
        if positive_int(declared, "Narration total_parts") != actual_parts:
            raise RuntimeError(
                "Narration total_parts does not match actual parts"
            )

    if mode == "full" and actual_parts != configured_parts:
        raise RuntimeError(
            f"FULL part count mismatch: configured="
            f"{configured_parts}, actual={actual_parts}"
        )

    expected_count = actual_parts * scenes

    if len(jobs) != expected_count:
        raise RuntimeError(
            f"Narration scene count mismatch: "
            f"expected={expected_count}, found={len(jobs)}"
        )

    log("FORMAT:", mode)
    log("CONFIGURED PARTS:", configured_parts)
    log("ACTUAL NARRATION PARTS:", actual_parts)
    log("SCENES PER PART:", scenes)
    log("EXPECTED NARRATION SCENES:", expected_count)

    return actual_parts


def load_narration(
    configured_parts,
    scenes,
    mode,
):
    data = read_json(AUDIO_FILE)
    jobs = rows_from(
        data, "jobs", "audio_jobs", "scenes"
    )

    if not jobs:
        raise RuntimeError(
            "No narration jobs found"
        )

    actual_parts = resolve_actual_parts(
        data,
        jobs,
        mode,
        configured_parts,
        scenes,
    )

    mapping = {}

    for index, row in enumerate(jobs, 1):
        try:
            key = (
                int(row["part"]),
                int(row["scene"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid narration mapping at row {index}"
            ) from exc

        if key in mapping:
            raise RuntimeError(
                f"Duplicate narration scene: {key}"
            )

        if str(row.get("status", "")).strip().lower() != "completed":
            raise RuntimeError(
                f"Narration is incomplete: {key}"
            )

        path = safe_file(
            first_value(row, PATH_KEYS),
            f"Narration {key}",
            MIN_AUDIO_BYTES,
        )

        info = media_info(path)
        actual_hash = sha256_file(path)
        declared_hash = row.get("sha256")

        if declared_hash and declared_hash != actual_hash:
            raise RuntimeError(
                f"Narration checksum mismatch: {key}"
            )

        mapping[key] = {
            "path": path,
            "relative_path": relative(path),
            "duration": info["duration"],
            "bytes": info["bytes"],
            "sha256": actual_hash,
        }

    expected = expected_keys(actual_parts, scenes)

    if set(mapping) != expected:
        raise RuntimeError(
            "Narration mapping mismatch: "
            f"missing={sorted(expected - set(mapping))}; "
            f"extra={sorted(set(mapping) - expected)}"
        )

    return mapping, actual_parts


def validate_sound_manifest(
    mode,
    parts,
    narration,
):
    manifest = read_json(SOUND_MANIFEST)

    if not isinstance(manifest, dict):
        raise RuntimeError(
            "Sound-design manifest must be an object"
        )

    if manifest.get("status") != "completed":
        raise RuntimeError(
            "Sound-design manifest is not completed"
        )

    manifest_format = str(
        manifest.get("format", "")
    ).strip().lower()

    if manifest_format != mode:
        raise RuntimeError(
            f"Sound-design FORMAT mismatch: "
            f"{manifest_format} != {mode}"
        )

    if positive_int(
        manifest.get("parts"),
        "Sound-design parts",
    ) != parts:
        raise RuntimeError(
            "Sound-design actual part count mismatch"
        )

    if positive_int(
        manifest.get("total_scenes"),
        "Sound-design total_scenes",
    ) != len(narration):
        raise RuntimeError(
            "Sound-design scene count mismatch"
        )

    enabled_tracks = manifest.get("enabled")

    if not isinstance(enabled_tracks, dict):
        raise RuntimeError(
            "Sound-design enabled settings are missing"
        )

    for name in ("music", "ambient", "sfx"):
        if not isinstance(enabled_tracks.get(name), bool):
            raise RuntimeError(
                f"Sound-design enabled.{name} must be boolean"
            )

    tracks = manifest.get("tracks")

    if not isinstance(tracks, dict):
        raise RuntimeError(
            "Sound-design tracks must be an object"
        )

    for category in ("music", "ambient"):
        record = tracks.get(category)

        if not enabled_tracks[category]:
            if record is not None:
                raise RuntimeError(
                    f"{category} is disabled but a track is declared"
                )
            continue

        if not isinstance(record, dict):
            raise RuntimeError(
                f"{category} is enabled but its track is missing"
            )

        if record.get("status") != "completed":
            raise RuntimeError(
                f"{category} track is not completed"
            )

        path = safe_file(
            record.get("path"),
            f"{category} track",
            MIN_AUDIO_BYTES,
        )

        actual_hash = sha256_file(path)

        if record.get("sha256") != actual_hash:
            raise RuntimeError(
                f"{category} track checksum mismatch"
            )

        info = media_info(path)

        total_duration = sum(
            row["duration"]
            for row in narration.values()
        )

        if info["duration"] + 0.5 < total_duration:
            raise RuntimeError(
                f"{category} track is shorter than narration: "
                f"{info['duration']:.2f}s < {total_duration:.2f}s"
            )

    return manifest


def validate_cues(
    parts,
    scenes,
    narration,
    sfx_enabled,
    sound_manifest,
):
    data = read_json(CUES_FILE)

    if not isinstance(data, dict):
        raise RuntimeError(
            "SFX cue manifest must be a JSON object"
        )

    if data.get("status") != "completed":
        raise RuntimeError(
            "SFX cue manifest is not completed"
        )

    cue_format = str(
        data.get("format", "")
    ).strip().lower()

    if cue_format != sound_manifest.get("format"):
        raise RuntimeError(
            "SFX cue and sound-design FORMAT mismatch"
        )

    if positive_int(
        data.get("actual_parts"),
        "SFX actual_parts",
    ) != parts:
        raise RuntimeError(
            "SFX cue actual_parts mismatch"
        )

    if positive_int(
        data.get("total_scenes"),
        "SFX total_scenes",
    ) != len(narration):
        raise RuntimeError(
            "SFX cue total_scenes mismatch"
        )

    cues = rows_from(data, "cues", "items")

    if not sfx_enabled and cues:
        raise RuntimeError(
            "SFX is disabled but stale cues remain. "
            "Regenerate sound design."
        )

    if sfx_enabled and not cues:
        raise RuntimeError(
            "SFX is enabled but the cue manifest contains zero cues"
        )

    seen = set()
    reports = []
    warnings = []
    per_scene = {}

    for index, cue in enumerate(cues, 1):
        if not isinstance(cue, dict):
            raise RuntimeError(
                f"Invalid SFX cue at row {index}"
            )

        try:
            key = (
                int(cue["part"]),
                int(cue["scene"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid SFX scene mapping at row {index}"
            ) from exc

        if key not in narration or not (
            1 <= key[0] <= parts
            and 1 <= key[1] <= scenes
        ):
            raise RuntimeError(
                f"SFX cue references unknown scene: {key}"
            )

        start = required_number(
            cue,
            TIME_KEYS,
            f"SFX cue {index} timestamp",
            minimum=0,
        )

        gain = required_number(
            cue,
            ("gain", "volume", "volume_gain"),
            f"SFX cue {index} gain",
            minimum=0,
            maximum=MAX_GAIN,
        )

        kind = (
            str(cue.get("kind", "unspecified")).strip()
            or "unspecified"
        )

        cue_id = (
            key,
            round(start, 6),
            kind,
        )

        if cue_id in seen:
            raise RuntimeError(
                f"Duplicate SFX cue: {cue_id}"
            )

        seen.add(cue_id)

        path = safe_file(
            first_value(cue, PATH_KEYS),
            f"SFX cue {index}",
            MIN_AUDIO_BYTES,
        )

        info = media_info(path)
        actual_hash = sha256_file(path)

        if cue.get("sha256") and cue["sha256"] != actual_hash:
            raise RuntimeError(
                f"SFX checksum mismatch: cue {index}"
            )

        declared_duration = cue.get(
            "effect_duration_seconds"
        )

        if declared_duration is not None:
            declared_duration = required_number(
                cue,
                ("effect_duration_seconds",),
                f"SFX cue {index} declared duration",
                minimum=0.01,
            )

            if abs(declared_duration - info["duration"]) > 0.15:
                warnings.append({
                    "part": key[0],
                    "scene": key[1],
                    "kind": kind,
                    "warning": (
                        "Declared SFX duration differs from "
                        "the actual audio file duration"
                    ),
                    "declared_seconds": round(
                        declared_duration, 3
                    ),
                    "actual_seconds": round(
                        info["duration"], 3
                    ),
                })

        scene_duration = narration[key]["duration"]

        if start >= scene_duration:
            raise RuntimeError(
                f"SFX begins outside narration scene {key}: "
                f"{start:.3f}s >= {scene_duration:.3f}s"
            )

        if not sfx_enabled:
            raise RuntimeError(
                "Unexpected SFX cue while SFX is disabled"
            )

        available = max(
            0.0,
            scene_duration - start,
        )

        effective = min(
            info["duration"],
            available,
        )

        truncated = max(
            0.0,
            info["duration"] - effective,
        )

        maximum_start = max(
            0.0,
            scene_duration - info["duration"] - 0.05,
        )

        renderer_start = min(
            start,
            maximum_start,
        )

        renderer_effective = min(
            info["duration"],
            max(0.0, scene_duration - renderer_start),
        )

        renderer_truncated = max(
            0.0,
            info["duration"] - renderer_effective,
        )

        if renderer_start + EPSILON < start:
            warnings.append({
                "part": key[0],
                "scene": key[1],
                "kind": kind,
                "declared_start_seconds": round(start, 4),
                "renderer_start_seconds": round(
                    renderer_start, 4
                ),
                "warning": (
                    "Renderer will shift this SFX earlier "
                    "to preserve its duration"
                ),
            })

        if renderer_truncated > 0.02:
            warnings.append({
                "part": key[0],
                "scene": key[1],
                "kind": kind,
                "truncated_seconds": round(
                    renderer_truncated, 4
                ),
                "warning": (
                    "SFX file is longer than the full narration scene"
                ),
            })

        report = {
            "part": key[0],
            "scene": key[1],
            "kind": kind,
            "start_seconds": round(start, 4),
            "end_seconds": round(start + effective, 4),
            "renderer_start_seconds": round(
                renderer_start, 4
            ),
            "renderer_end_seconds": round(
                renderer_start + renderer_effective, 4
            ),
            "gain": gain,
            "narration_duration_seconds": round(
                scene_duration, 4
            ),
            "file_duration_seconds": round(
                info["duration"], 4
            ),
            "effective_duration_seconds": round(
                effective, 4
            ),
            "renderer_effective_duration_seconds": round(
                renderer_effective, 4
            ),
            "truncated_seconds": round(truncated, 4),
            "renderer_truncated_seconds": round(
                renderer_truncated, 4
            ),
            "file": relative(path),
            "sha256": actual_hash,
            "timing_status": (
                "fits_scene"
                if renderer_truncated <= 0.02
                else "may_be_trimmed"
            ),
        }

        reports.append(report)
        per_scene.setdefault(key, []).append(report)

        if len(per_scene[key]) > MAX_CUES_PER_SCENE:
            raise RuntimeError(
                f"Too many SFX cues in scene {key}: "
                f"{len(per_scene[key])}"
            )

    if sfx_enabled:
        expected = set(narration)
        actual = set(per_scene)

        if actual != expected:
            raise RuntimeError(
                "SFX scene coverage mismatch: "
                f"missing={sorted(expected - actual)}"
            )

    reports.sort(
        key=lambda item: (
            item["part"],
            item["scene"],
            item["start_seconds"],
            item["kind"],
        )
    )

    overlap_warnings = []
    scene_reports = []

    for part in range(1, parts + 1):
        for scene in range(1, scenes + 1):
            key = (part, scene)

            scene_cues = sorted(
                per_scene.get(key, []),
                key=lambda item: item["renderer_start_seconds"],
            )

            overlaps = []

            for previous, current in zip(
                scene_cues,
                scene_cues[1:],
            ):
                overlap = (
                    previous["renderer_end_seconds"]
                    - current["renderer_start_seconds"]
                )

                if overlap > EPSILON:
                    item = {
                        "part": part,
                        "scene": scene,
                        "previous_kind": previous["kind"],
                        "next_kind": current["kind"],
                        "overlap_seconds": round(
                            overlap, 4
                        ),
                        "note": (
                            "Overlapping effects are permitted; "
                            "check that they do not mask narration."
                        ),
                    }

                    overlaps.append(item)
                    overlap_warnings.append(item)

            scene_reports.append({
                "part": part,
                "scene": scene,
                "narration_duration_seconds": round(
                    narration[key]["duration"], 4
                ),
                "sfx_cue_count": len(scene_cues),
                "overlap_count": len(overlaps),
                "overlaps": overlaps,
                "status": (
                    "has_sfx" if scene_cues else "no_sfx"
                ),
            })

    return {
        "status": "valid",
        "enabled": sfx_enabled,
        "cue_count": len(reports),
        "scenes_with_sfx": sum(
            bool(value) for value in per_scene.values()
        ),
        "scenes_without_sfx": (
            len(narration) - len(per_scene)
        ),
        "cues": reports,
        "scene_audit": scene_reports,
        "truncation_warnings": warnings,
        "overlap_warnings": overlap_warnings,
        "limitations": [
            "This validates cue timing and media files, not perceived loudness.",
            "The renderer still must mix the validated tracks correctly.",
            "This does not create word-level narration alignment.",
        ],
    }


def main():
    if not shutil.which("ffprobe"):
        raise RuntimeError(
            "ffprobe is required"
        )

    for path in (
        INPUT_FILE,
        AUDIO_FILE,
        SOUND_MANIFEST,
        CUES_FILE,
    ):
        if not path.is_file():
            raise RuntimeError(
                f"Required file missing: {path}"
            )

    config = load_and_validate(INPUT_FILE)

    mode = str(get_format(config)).strip().lower()
    configured_parts = positive_int(
        get_parts(config), "PARTS"
    )
    scenes = positive_int(
        get_scenes(config), "SCENES"
    )

    if mode not in {"short", "full"}:
        raise RuntimeError(
            f"Unsupported FORMAT: {mode}"
        )

    sfx_enabled = config_bool(
        config,
        SFX_KEYS,
        default=True,
    )

    narration, actual_parts = load_narration(
        configured_parts,
        scenes,
        mode,
    )

    sound_manifest = validate_sound_manifest(
        mode,
        actual_parts,
        narration,
    )

    manifest_sfx_enabled = sound_manifest[
        "enabled"
    ]["sfx"]

    if manifest_sfx_enabled != sfx_enabled:
        raise RuntimeError(
            "SFX_ENABLED in Input/topic.txt does not match "
            "sound-design manifest. Regenerate sound design."
        )

    sfx = validate_cues(
        actual_parts,
        scenes,
        narration,
        sfx_enabled,
        sound_manifest,
    )

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "configured_parts": configured_parts,
        "total_parts": actual_parts,
        "dynamic_short_parts": mode == "short",
        "scenes_per_part": scenes,
        "expected_scenes": actual_parts * scenes,
        "external_api_calls": 0,
        "paid_services_used": False,
        "sfx_enabled": sfx_enabled,
        "narration_scenes_checked": len(narration),
        "narration_total_seconds": round(
            sum(
                row["duration"]
                for row in narration.values()
            ),
            3,
        ),
        "duration_source": (
            "ffprobe duration of actual narration files"
        ),
        "sound_design_manifest_sha256": sha256_file(
            SOUND_MANIFEST
        ),
        "sfx_cue_manifest_sha256": sha256_file(
            CUES_FILE
        ),
        "narration_manifest_sha256": sha256_file(
            AUDIO_FILE
        ),
        "sound_tracks": {
            "music_enabled": sound_manifest["enabled"]["music"],
            "ambient_enabled": sound_manifest["enabled"]["ambient"],
            "sfx_enabled": sfx_enabled,
        },
        "sfx": sfx,
    }

    write_json(OUTPUT_FILE, report)

    log("=" * 60)
    log("SFX TIMING VALIDATION: PASSED")
    log("VERSION:", VERSION)
    log("FORMAT:", mode)
    log("CONFIGURED PARTS:", configured_parts)
    log("ACTUAL PARTS:", actual_parts)
    log("NARRATION SCENES:", len(narration))
    log("NARRATION DURATION:", report["narration_total_seconds"])
    log("SFX ENABLED:", sfx_enabled)
    log("SFX CUES:", sfx["cue_count"])
    log("SCENES WITHOUT SFX:", sfx["scenes_without_sfx"])
    log("TIMING WARNINGS:", len(sfx["truncation_warnings"]))
    log("OVERLAPPING CUE PAIRS:", len(sfx["overlap_warnings"]))
    log("REPORT:", relative(OUTPUT_FILE))
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("SFX TIMING VALIDATION: FAILED:", str(exc))
        sys.exit(1)

