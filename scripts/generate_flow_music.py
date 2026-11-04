#!/usr/bin/env python3
"""Story-aware independent Google Lyria compositions with verified resume."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = ROOT / "Input" / "topic.txt"
STORY_FILE = ROOT / "output" / "story" / "story.json"
AUDIO_JOBS = ROOT / "output" / "narration" / "audio_jobs.json"

AUDIO_DIR = ROOT / "output" / "music"
SOURCE_DIR = AUDIO_DIR / "role_sources"
SOURCE_AUDIO = AUDIO_DIR / "flow_music_source.audio"
FINAL_AUDIO = AUDIO_DIR / "background_music.mp3"
SEGMENT_DIR = AUDIO_DIR / "role_segments"
MANIFEST = AUDIO_DIR / "flow_music_manifest.json"
CHECKPOINT = ROOT / "output" / "checkpoints" / "flow_music_progress.json"

API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_MODEL = "lyria-3.5"
VERSION = "photo-motion-lyria-independent-role-compositions-v13"

REQUEST_TIMEOUT = (30, 900)
RETRY_DELAYS = (8, 20)
MAX_PROMPT_CHARS = 6500
MIN_AUDIO_BYTES = 10000
MIN_AUDIO_DURATION = 10.0
MAX_AUDIO_BYTES = 100 * 1024 * 1024
DURATION_TOLERANCE = 0.65

AUDIO_PATH_KEYS = (
    "output", "audio_path", "audio_file",
    "path", "file", "audio",
)

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}
SEGMENT_ROLES = ("hook", "main", "suspense", "resolution")

API_CALLS = 0
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "KathaLokAI-PhotoMotion/13.0",
})


def log(*values):
    print(*values, flush=True)


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def truthy(value, default=False):
    if value is None:
        return default
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "on", "enabled"}:
        return True
    if value in {"0", "false", "no", "off", "disabled"}:
        return False
    raise RuntimeError(f"Invalid boolean setting: {value!r}")


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(data):
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_settings():
    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    result = {}
    for raw in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        result[key.strip().upper()] = value.strip()
    return result


def setting(config, names, default=None):
    for name in names:
        if name in config:
            return config[name]
    return default


def project_path(value):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("Audio manifest contains an empty path.")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    root = ROOT.resolve()
    if path != root and root not in path.parents:
        raise RuntimeError("Audio path escapes the project directory.")
    return path


def run(command, timeout=180):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): "
            f"{(result.stderr or result.stdout or '')[-2000:]}"
        )
    return result


def probe_audio(path, minimum=0.01):
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required.")

    path = Path(path)
    if (
        not path.is_file()
        or path.stat().st_size < MIN_AUDIO_BYTES
        or path.stat().st_size > MAX_AUDIO_BYTES
    ):
        raise RuntimeError(f"Audio file is missing, empty or oversized: {path}")

    result = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "json", str(path),
    ], 90)

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        has_audio = any(
            row.get("codec_type") == "audio"
            for row in data.get("streams", [])
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Invalid audio metadata: {path}") from exc

    if not math.isfinite(duration) or duration < minimum or not has_audio:
        raise RuntimeError(f"Audio is empty or invalid: {path}")
    return duration


def measure_audio(path):
    result = run([
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(path),
        "-af", "ebur128=peak=true",
        "-f", "null", "-",
    ], 180)

    output = result.stderr or ""
    patterns = {
        "integrated_lufs": r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS",
        "loudness_range_lu": r"LRA:\s*(-?\d+(?:\.\d+)?)\s*LU",
        "true_peak_dbtp": r"Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS",
    }
    measurements = {}
    for key, pattern in patterns.items():
        matches = re.findall(pattern, output)
        if matches:
            measurements[key] = float(matches[-1])

    if "integrated_lufs" not in measurements:
        raise RuntimeError("Could not measure integrated music loudness.")
    return measurements


def read_story():
    story = read_json(STORY_FILE)
    if not story:
        raise RuntimeError("output/story/story.json is missing or invalid.")

    title = clean(
        story.get("title")
        or story.get("final_title")
        or story.get("generated_title")
        or story.get("topic")
    )
    story_text = clean(
        story.get("corrected_complete_story")
        or story.get("story_text")
        or story.get("final_story")
        or story.get("source_text")
        or story.get("text")
        or story.get("content")
    )
    summary = clean(
        story.get("summary")
        or story.get("story_summary")
        or story.get("synopsis")
        or story.get("description")
    )

    if not title and not story_text and not summary:
        raise RuntimeError("Story title, text and summary are all empty.")
    if not story_text and not summary:
        raise RuntimeError("Complete story text or story summary is required.")

    return title or "Untitled story", story_text, summary


def read_narration_jobs():
    manifest = read_json(AUDIO_JOBS)
    if manifest.get("status") != "completed":
        raise RuntimeError("TTS audio_jobs.json is not completed.")

    jobs = manifest.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("TTS audio_jobs.json contains no jobs.")

    rows = []
    seen = set()

    for row in jobs:
        if (
            not isinstance(row, dict)
            or row.get("status") not in {"completed", "reused"}
        ):
            raise RuntimeError("TTS manifest contains an incomplete job.")

        scene_id = clean(row.get("scene_id") or row.get("job_id"))
        if not scene_id or scene_id in seen:
            raise RuntimeError(f"Missing or duplicate TTS scene_id: {scene_id}")
        seen.add(scene_id)

        role = clean(row.get("role") or "main").lower()
        if role not in ROLE_ORDER:
            raise RuntimeError(f"Unsupported narration role: {role}")

        try:
            part = int(row["part"])
            scene = int(row["scene"]) if role == "main" else None
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid narration identity: {scene_id}") from exc

        if part < 1 or (role == "main" and (scene is None or scene < 1)):
            raise RuntimeError(f"Invalid narration identity: {scene_id}")

        audio_value = next(
            (
                row.get(key)
                for key in AUDIO_PATH_KEYS
                if isinstance(row.get(key), str) and row[key].strip()
            ),
            None,
        )
        path = project_path(audio_value)
        duration = probe_audio(path)
        checksum = sha256_file(path)

        if clean(row.get("sha256")).lower() != checksum.lower():
            raise RuntimeError(f"TTS SHA256 mismatch: {scene_id}")

        declared = row.get(
            "actual_audio_duration_seconds",
            row.get("duration"),
        )
        try:
            declared = float(declared)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Measured duration missing: {scene_id}") from exc

        if not math.isfinite(declared) or abs(declared - duration) > 0.20:
            raise RuntimeError(f"TTS duration mismatch: {scene_id}")

        duration_source = (
            row.get("duration_source")
            or row.get("audio_duration_source")
        )
        if duration_source != "ffprobe_measured":
            raise RuntimeError(f"Unverified TTS duration: {scene_id}")

        rows.append({
            "scene_id": scene_id,
            "part": part,
            "scene": scene,
            "role": role,
            "duration_seconds": duration,
            "audio_path": path.relative_to(ROOT).as_posix(),
            "audio_sha256": checksum,
            "text": clean(row.get("text") or row.get("narration")),
        })

    rows.sort(key=lambda row: (
        row["part"],
        ROLE_ORDER[row["role"]],
        row["scene"] or 0,
        row["scene_id"],
    ))
    return rows


def parse_scenes_range(config):
    raw = clean(
        config.get("SCENES_SPEC")
        or config.get("SCENES")
        or "1"
    )
    match = re.fullmatch(r"(\d+)\s*(?:-\s*(\d+))?", raw)
    if not match:
        raise RuntimeError(f"Invalid SCENES range: {raw!r}")

    minimum = int(match.group(1))
    maximum = int(match.group(2) or match.group(1))
    if minimum < 1 or maximum < minimum or maximum > 500:
        raise RuntimeError(f"Invalid SCENES range: {raw!r}")

    supplied_min = config.get("SCENES_MIN")
    supplied_max = config.get("SCENES_MAX")
    if supplied_min not in (None, "") and supplied_max not in (None, ""):
        try:
            supplied = (int(supplied_min), int(supplied_max))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("SCENES_MIN and SCENES_MAX must be integers.") from exc
        if supplied != (minimum, maximum):
            raise RuntimeError(
                "SCENES range mismatch: "
                f"SCENES_SPEC={minimum}-{maximum}, "
                f"SCENES_MIN={supplied[0]}, SCENES_MAX={supplied[1]}."
            )
    return minimum, maximum


def validate_role_structure(rows, config):
    if not rows:
        raise RuntimeError("No completed narration audio records.")

    parts = sorted({row["part"] for row in rows})
    if not parts or parts != list(range(parts[0], parts[-1] + 1)):
        raise RuntimeError("Narration part numbering is not continuous.")

    scenes_min, scenes_max = parse_scenes_range(config)
    by_part = {}
    for row in rows:
        by_part.setdefault(row["part"], []).append(row)

    final_part = parts[-1]
    actual_counts = {}

    for part in parts:
        group = by_part[part]
        roles = [row["role"] for row in group]

        if roles.count("hook") != 1:
            raise RuntimeError(f"Part {part} must have exactly one Hook.")
        if roles.count("suspense") != (1 if part != final_part else 0):
            raise RuntimeError(f"Part {part} has invalid Suspense records.")
        if roles.count("resolution") != (1 if part == final_part else 0):
            raise RuntimeError(f"Part {part} has invalid Resolution records.")

        main = sorted(
            row["scene"] for row in group if row["role"] == "main"
        )
        if (
            len(main) < scenes_min
            or len(main) > scenes_max
            or main != list(range(1, len(main) + 1))
        ):
            raise RuntimeError(
                f"Part {part} Main scenes {main} are outside "
                f"the configured range {scenes_min}-{scenes_max}."
            )
        actual_counts[str(part)] = len(main)

    expected_count = (
        sum(actual_counts.values())
        + len(parts)
        + max(0, len(parts) - 1)
        + 1
    )
    if len(rows) != expected_count:
        raise RuntimeError(
            f"Narration record count mismatch: expected {expected_count}, "
            f"found {len(rows)}."
        )

    return {
        "parts": parts,
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "actual_scene_counts": actual_counts,
        "main_count": sum(actual_counts.values()),
        "hook_count": len(parts),
        "suspense_count": max(0, len(parts) - 1),
        "resolution_count": 1,
    }


def build_timeline(rows):
    offset = 0.0
    timeline = []
    for row in rows:
        duration = row["duration_seconds"]
        end = offset + duration
        timeline.append({
            "scene_id": row["scene_id"],
            "part": row["part"],
            "scene": row["scene"],
            "role": row["role"],
            "start_seconds": round(offset, 4),
            "duration_seconds": round(duration, 4),
            "end_seconds": round(end, 4),
            "audio_path": row["audio_path"],
            "audio_sha256": row["audio_sha256"],
        })
        offset = end
    return timeline, round(offset, 4)


def story_context(story_text, summary):
    context = clean((summary + " " + story_text).strip())
    if len(context) <= MAX_PROMPT_CHARS:
        return context

    limit = MAX_PROMPT_CHARS - 500
    first = int(limit * 0.38)
    last = int(limit * 0.34)
    middle = limit - first - last
    start = max(0, (len(context) - middle) // 2)

    return clean(
        context[:first]
        + " [middle story events] "
        + context[start:start + middle]
        + " [complete ending and resolution] "
        + context[-last:]
    )


def build_role_prompt(title, story_text, summary, role, rows, config):
    style = clean(config.get("MUSIC_STYLE", "cinematic")) or "cinematic"
    genre = clean(config.get("MUSIC_GENRE", "original film score"))
    mood = clean(config.get("MOOD", "dramatic")) or "dramatic"
    intensity = clean(config.get("MUSIC_INTENSITY", "evolving"))
    role_rows = [row for row in rows if row["role"] == role]
    narration = " ".join(row["text"] for row in role_rows if row["text"])
    narration = narration[:1800]
    story = story_context(story_text, summary)

    directions = {
        "hook": (
            "Create an intriguing, immediately engaging opening. "
            "Use restrained mystery and a memorable opening motif."
        ),
        "main": (
            "Create a sustained narrative underscore for the main story. "
            "Support emotional changes while leaving narration intelligible."
        ),
        "suspense": (
            "Create a distinct tension-building suspense composition. "
            "Increase anticipation gradually; avoid sudden harsh peaks."
        ),
        "resolution": (
            "Create an emotionally satisfying resolution composition. "
            "Give the ending a clear musical landing and natural finish."
        ),
    }

    duration = sum(row["duration_seconds"] for row in role_rows)
    return (
        "Create an original instrumental cinematic composition for a Hindi "
        "storytelling video. This is an INDEPENDENT composition for the "
        f"{role.upper()} role, not an excerpt from another role's music. "
        "Do not copy or imitate existing songs, artists or soundtracks. "
        "No vocals, lyrics, speech or chanting. Avoid repetitive short loops, "
        "abrupt cuts and long silent gaps. Keep bass controlled and the "
        "speech midrange clear. Use a coherent story-related musical identity "
        "but give this role its own arrangement, rhythm and emotional shape. "
        f"{directions[role]} "
        f"Story title: {title}. Style: {style}. Genre: {genre}. "
        f"Mood: {mood}. Intensity: {intensity}. "
        f"Target role duration: {duration:.2f} seconds. "
        f"Story context: {story}. "
        f"{role.upper()} narration context: {narration}"
    )


def decode_audio_block(block):
    if not isinstance(block, dict):
        return None
    encoded = block.get("data")
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None


def extract_audio(data):
    if not isinstance(data, dict):
        raise RuntimeError("Google API returned a non-object response.")

    candidates = []
    if isinstance(data.get("output_audio"), dict):
        candidates.append(data["output_audio"])

    for key in ("outputs", "output", "steps"):
        items = data.get(key, [])
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") in {"audio", "output_audio"}:
                candidates.append(item)

            content = item.get("content", [])
            if isinstance(content, dict):
                content = [content]
            if isinstance(content, list):
                candidates.extend(
                    block for block in content
                    if isinstance(block, dict)
                    and block.get("type") in {"audio", "output_audio"}
                )

    for block in candidates:
        audio = decode_audio_block(block)
        if audio and MIN_AUDIO_BYTES <= len(audio) <= MAX_AUDIO_BYTES:
            return audio

    raise RuntimeError("Google response contains no valid audio data block.")


def request_music(api_key, model, prompt, destination):
    global API_CALLS

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    payload = {"model": model, "input": prompt}
    last_error = "Unknown Google Lyria API error."

    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            API_CALLS += 1
            response = SESSION.post(
                API_URL,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code in {408, 429, 500, 502, 503, 504}:
                last_error = (
                    f"Temporary Google API error {response.status_code}: "
                    f"{response.text[:800]}"
                )
                if attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                break

            if response.status_code >= 400:
                raise RuntimeError(
                    f"Google API rejected request ({response.status_code}): "
                    f"{response.text[:1200]}"
                )

            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError("Google API returned invalid JSON.") from exc

            audio = extract_audio(data)
            temporary = destination.with_name(destination.name + ".tmp")
            temporary.parent.mkdir(parents=True, exist_ok=True)
            try:
                temporary.write_bytes(audio)
                probe_audio(temporary, MIN_AUDIO_DURATION)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

            return probe_audio(destination, MIN_AUDIO_DURATION)

        except requests.Timeout as exc:
            last_error = f"Google music request timed out: {exc}"
        except requests.ConnectionError as exc:
            last_error = f"Google music connection failed: {exc}"
        except requests.RequestException as exc:
            raise RuntimeError(f"Google music request failed: {exc}") from exc

        if attempt < len(RETRY_DELAYS):
            time.sleep(RETRY_DELAYS[attempt])

    raise RuntimeError(last_error)


def validate_model(config):
    model = clean(
        config.get("FLOW_MUSIC_MODEL")
        or os.environ.get("FLOW_MUSIC_MODEL")
        or DEFAULT_MODEL
    )
    if (
        not model
        or len(model) > 200
        or any(character.isspace() for character in model)
    ):
        raise RuntimeError("FLOW_MUSIC_MODEL is invalid.")
    return model


def billable_allowed(config):
    global_permission = truthy(
        os.environ.get(
            "ALLOW_BILLABLE_PROVIDERS",
            config.get("ALLOW_BILLABLE_PROVIDERS", "true"),
        ),
        True,
    )
    music_permission = truthy(
        config.get("MUSIC_ALLOW_BILLABLE", "true"),
        True,
    )
    return global_permission and music_permission


def source_fingerprint(
    title, story_text, summary, rows, config, model,
    target_duration, prompts, role_durations,
):
    return fingerprint({
        "version": VERSION,
        "provider": "google_lyria",
        "model": model,
        "title": title,
        "story_text": story_text,
        "summary": summary,
        "narration": [
            {
                "scene_id": row["scene_id"],
                "part": row["part"],
                "scene": row["scene"],
                "role": row["role"],
                "duration": round(row["duration_seconds"], 4),
                "audio_sha256": row["audio_sha256"],
            }
            for row in rows
        ],
        "settings": {
            key: config.get(key)
            for key in (
                "SCENES_SPEC", "SCENES_MIN", "SCENES_MAX",
                "MUSIC", "MUSIC_ENABLED", "MUSIC_STYLE",
                "MUSIC_GENRE", "MUSIC_INTENSITY", "MOOD",
                "FLOW_MUSIC_MODEL", "MUSIC_ALLOW_BILLABLE",
                "ALLOW_BILLABLE_PROVIDERS",
            )
        },
        "role_durations": role_durations,
        "target_duration": round(target_duration, 3),
        "prompts": prompts,
    })


def prepare_audio(source, destination, duration, bitrate="192k"):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required.")

    source_duration = probe_audio(source, MIN_AUDIO_DURATION)
    fade = min(5.0, max(0.8, duration * 0.04))
    fade_start = max(0.0, duration - fade)
    temporary = destination.with_name(destination.stem + ".building.mp3")
    temporary.unlink(missing_ok=True)

    try:
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-stream_loop", "-1", "-i", str(source),
            "-t", f"{duration:.3f}", "-vn",
            "-af",
            (
                "aresample=44100,"
                "loudnorm=I=-25:TP=-3.0:LRA=10,"
                f"afade=t=out:st={fade_start:.3f}:d={fade:.3f}"
            ),
            "-c:a", "libmp3lame", "-b:a", bitrate,
            "-ar", "44100", "-ac", "2",
            "-map_metadata", "-1", str(temporary),
        ], 900)

        actual = probe_audio(temporary, MIN_AUDIO_DURATION)
        if abs(actual - duration) > DURATION_TOLERANCE:
            raise RuntimeError(
                f"Music duration mismatch: {actual:.3f}s vs {duration:.3f}s."
            )
        os.replace(temporary, destination)
        return {
            "duration_seconds": actual,
            "source_duration_seconds": source_duration,
            "sha256": sha256_file(destination),
            "size_bytes": destination.stat().st_size,
            "loudness": measure_audio(destination),
        }
    finally:
        temporary.unlink(missing_ok=True)


def extract_segment(source, destination, start, duration):
    if duration <= 0.05:
        raise RuntimeError("Music segment duration is invalid.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".building.mp3")
    temporary.unlink(missing_ok=True)

    fade_in = min(0.20, duration / 8)
    fade_out = min(0.35, duration / 6)
    fade_start = max(0.0, duration - fade_out)

    try:
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.4f}", "-i", str(source),
            "-t", f"{duration:.4f}", "-vn",
            "-af",
            (
                f"afade=t=in:d={fade_in:.4f},"
                f"afade=t=out:st={fade_start:.4f}:d={fade_out:.4f}"
            ),
            "-c:a", "libmp3lame", "-b:a", "160k",
            "-ar", "44100", "-ac", "2", str(temporary),
        ], 300)

        actual = probe_audio(temporary, 0.05)
        if abs(actual - duration) > DURATION_TOLERANCE:
            raise RuntimeError(
                f"Role segment duration mismatch: {actual:.3f}s "
                f"vs {duration:.3f}s."
            )
        os.replace(temporary, destination)
        return {
            "path": destination.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(destination),
            "duration_seconds": round(actual, 4),
            "start_seconds": round(start, 4),
            "loudness": measure_audio(destination),
        }
    finally:
        temporary.unlink(missing_ok=True)

def build_role_compositions(
    api_key,
    model,
    prompts,
    role_durations,
    role_rows,
):
    compositions = {}

    for role in SEGMENT_ROLES:
        duration = role_durations.get(role, 0.0)
        if duration <= 0:
            continue

        SOURCE_DIR.mkdir(parents=True, exist_ok=True)
        source = SOURCE_DIR / f"{role}_source.audio"
        prepared = SOURCE_DIR / f"{role}_composition.mp3"

        # Each role has its own source composition and independent prompt.
        source_duration = request_music(
            api_key,
            model,
            prompts[role],
            source,
        )

        target_duration = max(10.0, duration)
        prepared_info = prepare_audio(
            source,
            prepared,
            target_duration,
        )

        compositions[role] = {
            "role": role,
            "source_path": source.relative_to(ROOT).as_posix(),
            "source_sha256": sha256_file(source),
            "source_duration_seconds": round(source_duration, 4),
            "path": prepared.relative_to(ROOT).as_posix(),
            "sha256": prepared_info["sha256"],
            "duration_seconds": round(
                prepared_info["duration_seconds"],
                4,
            ),
            "required_role_duration_seconds": round(duration, 4),
            "loudness": prepared_info["loudness"],
            "independent_composition": True,
        }

    return compositions


def build_role_segments(compositions, timeline):
    offsets = {role: 0.0 for role in SEGMENT_ROLES}
    segments = []
    grouped = {role: [] for role in SEGMENT_ROLES}

    for row in timeline:
        role = row["role"]
        if role not in compositions:
            raise RuntimeError(
                f"Missing independent music composition for role: {role}"
            )

        composition = compositions[role]
        source = project_path(composition["path"])
        role_offset = offsets[role]
        duration = row["duration_seconds"]

        if role_offset + duration > composition["duration_seconds"] + 0.30:
            raise RuntimeError(
                f"Independent {role} composition is too short for "
                f"narration record {row['scene_id']}."
            )

        safe_name = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "_",
            row["scene_id"],
        ).strip("_")
        if not safe_name:
            raise RuntimeError(
                f"Invalid narration scene ID for music segment: "
                f"{row['scene_id']}"
            )

        target = SEGMENT_DIR / f"{safe_name}.mp3"
        item = extract_segment(
            source,
            target,
            role_offset,
            duration,
        )

        item.update({
            "scene_id": row["scene_id"],
            "part": row["part"],
            "role": role,
            "scene": row["scene"],
            "narration_sha256": row["audio_sha256"],
            "start_seconds": row["start_seconds"],
            "role_source_start_seconds": round(role_offset, 4),
            "composition_sha256": composition["sha256"],
            "segment_type": "independent_role_composition_excerpt",
        })

        segments.append(item)
        grouped[role].append(item)
        offsets[role] += duration

    role_summary = {}
    for role, items in grouped.items():
        role_summary[role] = {
            "count": len(items),
            "segments": items,
            "total_duration_seconds": round(
                sum(item["duration_seconds"] for item in items),
                4,
            ),
            "independent_composition": role in compositions,
            "composition_path": (
                compositions[role]["path"]
                if role in compositions
                else None
            ),
            "composition_sha256": (
                compositions[role]["sha256"]
                if role in compositions
                else None
            ),
        }

    return segments, role_summary


def build_master_from_segments(segments, duration):
    if not segments:
        raise RuntimeError("Cannot build master music without role segments.")

    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required.")

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    concat_file = AUDIO_DIR / "flow_music_master_segments.txt"
    temporary = FINAL_AUDIO.with_name(
        FINAL_AUDIO.stem + ".building.mp3"
    )
    temporary.unlink(missing_ok=True)

    try:
        with concat_file.open("w", encoding="utf-8", newline="\n") as handle:
            for item in segments:
                path = project_path(item["path"])
                if not path.is_file():
                    raise RuntimeError(
                        f"Music segment is missing: {item['scene_id']}"
                    )
                resolved = str(path.resolve())
                if "\n" in resolved or "\r" in resolved:
                    raise RuntimeError("Invalid newline in music segment path.")
                handle.write(
                    "file '" + resolved.replace("'", "'\\''") + "'\n"
                )

        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-af",
            (
                "aresample=44100,"
                "loudnorm=I=-25:TP=-3.0:LRA=10,"
                f"apad=pad_dur=3,atrim=duration={duration:.3f},"
                f"afade=t=out:st={max(0.0, duration - 3.0):.3f}:d=3"
            ),
            "-c:a", "libmp3lame", "-b:a", "192k",
            "-ar", "44100", "-ac", "2",
            "-map_metadata", "-1",
            str(temporary),
        ], 900)

        actual = probe_audio(temporary, MIN_AUDIO_DURATION)
        if abs(actual - duration) > DURATION_TOLERANCE:
            raise RuntimeError(
                f"Master duration mismatch: {actual:.3f}s vs {duration:.3f}s."
            )

        os.replace(temporary, FINAL_AUDIO)
        return {
            "duration_seconds": round(actual, 4),
            "sha256": sha256_file(FINAL_AUDIO),
            "size_bytes": FINAL_AUDIO.stat().st_size,
            "loudness": measure_audio(FINAL_AUDIO),
        }
    finally:
        temporary.unlink(missing_ok=True)
        concat_file.unlink(missing_ok=True)


def verify_segment_records(segments, timeline, narration_by_id):
    expected_ids = {row["scene_id"] for row in timeline}
    actual_ids = [item.get("scene_id") for item in segments]

    if (
        len(actual_ids) != len(set(actual_ids))
        or set(actual_ids) != expected_ids
    ):
        raise RuntimeError(
            "Role segment records do not cover every narration record."
        )

    timeline_by_id = {
        row["scene_id"]: row for row in timeline
    }

    for item in segments:
        scene_id = item["scene_id"]
        narration = narration_by_id.get(scene_id)
        timeline_row = timeline_by_id.get(scene_id)

        if narration is None or timeline_row is None:
            raise RuntimeError(
                f"Music segment has no narration record: {scene_id}"
            )

        if item.get("narration_sha256") != narration["audio_sha256"]:
            raise RuntimeError(
                f"Music segment narration hash mismatch: {scene_id}"
            )

        if item.get("role") != narration["role"]:
            raise RuntimeError(
                f"Music segment role mismatch: {scene_id}"
            )

        if int(item.get("part", 0)) != narration["part"]:
            raise RuntimeError(
                f"Music segment Part mismatch: {scene_id}"
            )

        if abs(
            float(item.get("start_seconds", -999))
            - timeline_row["start_seconds"]
        ) > 0.05:
            raise RuntimeError(
                f"Music segment timeline mismatch: {scene_id}"
            )

        segment_path = project_path(item.get("path"))
        if (
            not segment_path.is_file()
            or sha256_file(segment_path) != item.get("sha256")
        ):
            raise RuntimeError(
                f"Music segment SHA256 mismatch: {scene_id}"
            )

        actual_duration = probe_audio(segment_path, 0.05)
        if abs(
            actual_duration - narration["duration_seconds"]
        ) > DURATION_TOLERANCE:
            raise RuntimeError(
                f"Music segment is not narration-aligned: {scene_id}"
            )


def verify_compositions(compositions):
    for role, item in compositions.items():
        if role not in SEGMENT_ROLES:
            raise RuntimeError(
                f"Unknown role composition: {role}"
            )

        source = project_path(item.get("source_path"))
        prepared = project_path(item.get("path"))

        if not source.is_file() or not prepared.is_file():
            raise RuntimeError(
                f"Independent {role} composition file is missing."
            )

        if sha256_file(source) != item.get("source_sha256"):
            raise RuntimeError(
                f"Independent {role} source SHA256 mismatch."
            )

        if sha256_file(prepared) != item.get("sha256"):
            raise RuntimeError(
                f"Independent {role} composition SHA256 mismatch."
            )

        probe_audio(source, MIN_AUDIO_DURATION)
        probe_audio(prepared, MIN_AUDIO_DURATION)


def validate_master_loudness(measurements):
    integrated = measurements.get("integrated_lufs")
    peak = measurements.get("true_peak_dbtp")

    if integrated is None or peak is None:
        raise RuntimeError(
            "Master music loudness/true-peak measurements are incomplete."
        )
    if peak > -1.0:
        raise RuntimeError("Master music true peak exceeds safe limit.")
    if integrated > -20.0:
        raise RuntimeError(
            "Master music is too loud for narration headroom."
        )


def build_role_counts(role_summary):
    return {
        "hook": role_summary["hook_count"],
        "main": role_summary["main_count"],
        "suspense": role_summary["suspense_count"],
        "resolution": role_summary["resolution_count"],
    }


def verify_existing(signature, duration, timeline):
    previous = read_json(MANIFEST)

    if (
        previous.get("status") != "completed"
        or previous.get("version") != VERSION
        or previous.get("source_fingerprint") != signature
        or previous.get("provider") != "google_lyria"
        or previous.get("generation_method")
        != "four_independent_role_compositions"
    ):
        return False

    try:
        path = project_path(previous.get("path") or previous.get("audio_path"))
        if path != FINAL_AUDIO.resolve():
            return False

        actual = probe_audio(path, MIN_AUDIO_DURATION)
        if previous.get("sha256") != sha256_file(path):
            return False
        if abs(actual - duration) > DURATION_TOLERANCE:
            return False
        if abs(
            float(previous.get("required_duration_seconds"))
            - duration
        ) > 0.01:
            return False

        compositions = previous.get("role_compositions")
        segments = previous.get("role_segments")
        if not isinstance(compositions, dict) or not compositions:
            return False
        if not isinstance(segments, list):
            return False

        verify_compositions(compositions)
        narration_by_id = {
            row["scene_id"]: row for row in timeline
        }
        verify_segment_records(
            segments,
            timeline,
            narration_by_id,
        )

        log(
            "RESUME: verified independent role compositions, "
            "master audio and narration-aligned segments reused."
        )
        return True

    except Exception as exc:
        log("RESUME REJECTED:", str(exc)[:500])
        return False


def write_unavailable(
    reason,
    model=None,
    duration=None,
    signature=None,
):
    record = {
        "status": "unavailable",
        "provider": "google_lyria",
        "product": "Google Lyria",
        "model": model or DEFAULT_MODEL,
        "reason": str(reason)[:2000],
        "fallback": "sound_design_local_fallback",
        "paid_services_used": API_CALLS > 0,
        "external_api_calls": API_CALLS,
        "version": VERSION,
    }
    if duration is not None:
        record["required_duration_seconds"] = round(float(duration), 3)
    if signature:
        record["source_fingerprint"] = signature

    write_json(MANIFEST, record)
    write_json(CHECKPOINT, {
        "status": "unavailable",
        "version": VERSION,
        "reason": str(reason)[:2000],
        "source_fingerprint": signature,
        "api_calls": API_CALLS,
        "paid_services_used": API_CALLS > 0,
        "updated_at": time.time(),
    })


def write_disabled():
    write_json(MANIFEST, {
        "status": "disabled",
        "provider": "google_lyria",
        "reason": "Background music is disabled in Input/topic.txt.",
        "paid_services_used": False,
        "external_api_calls": 0,
        "version": VERSION,
    })
    write_json(CHECKPOINT, {
        "status": "disabled",
        "version": VERSION,
    })


def get_api_key():
    return (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()


def validate_published_manifest(
    signature,
    final_checksum,
    duration,
    timeline,
):
    published = read_json(MANIFEST)

    if (
        published.get("status") != "completed"
        or published.get("version") != VERSION
        or published.get("sha256") != final_checksum
        or published.get("source_fingerprint") != signature
        or published.get("path")
        != FINAL_AUDIO.relative_to(ROOT).as_posix()
        or published.get("generation_method")
        != "four_independent_role_compositions"
    ):
        raise RuntimeError(
            "Published Flow Music manifest failed verification."
        )

    actual = probe_audio(FINAL_AUDIO, MIN_AUDIO_DURATION)
    if abs(actual - duration) > DURATION_TOLERANCE:
        raise RuntimeError("Published master duration mismatch.")

    compositions = published.get("role_compositions")
    segments = published.get("role_segments")
    if not isinstance(compositions, dict) or not compositions:
        raise RuntimeError("Published role compositions are missing.")
    if not isinstance(segments, list):
        raise RuntimeError("Published role segment list is missing.")

    verify_compositions(compositions)
    narration_by_id = {
        row["scene_id"]: row for row in timeline
    }
    verify_segment_records(
        segments,
        timeline,
        narration_by_id,
    )

    if len(segments) != len(timeline):
        raise RuntimeError("Published role segment count mismatch.")

    return published


def main():
    global API_CALLS

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    SEGMENT_DIR.mkdir(parents=True, exist_ok=True)

    config = read_settings()
    enabled = truthy(
        setting(
            config,
            ("MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC"),
            "true",
        ),
        True,
    )

    if not enabled:
        write_disabled()
        log("MUSIC DISABLED.")
        return 0

    model = validate_model(config)

    if not billable_allowed(config):
        write_unavailable(
            "Billable music generation is disabled by configuration.",
            model=model,
        )
        log("LYRIA SKIPPED: billable permission is disabled.")
        return 0

    api_key = get_api_key()
    if not api_key:
        write_unavailable(
            "GEMINI_API_KEY and GOOGLE_API_KEY are not configured.",
            model=model,
        )
        log("LYRIA UNAVAILABLE: Google API key is missing.")
        return 0

    target_duration = None
    source_sig = None

    try:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("ffmpeg and ffprobe are required.")

        title, story_text, summary = read_story()
        rows = read_narration_jobs()
        role_summary = validate_role_structure(rows, config)

        narration_duration = sum(
            row["duration_seconds"] for row in rows
        )
        if not math.isfinite(narration_duration) or narration_duration <= 0:
            raise RuntimeError("Measured narration duration is invalid.")

        target_duration = round(narration_duration + 3.0, 3)
        timeline, timeline_duration = build_timeline(rows)

        if abs(timeline_duration - narration_duration) > 0.05:
            raise RuntimeError("Narration timeline duration mismatch.")

        role_rows = {
            role: [row for row in rows if row["role"] == role]
            for role in SEGMENT_ROLES
        }
        role_durations = {
            role: round(
                sum(row["duration_seconds"] for row in role_rows[role]),
                4,
            )
            for role in SEGMENT_ROLES
        }

        prompts = {
            role: build_role_prompt(
                title,
                story_text,
                summary,
                role,
                rows,
                config,
            )
            for role in SEGMENT_ROLES
            if role_durations[role] > 0
        }

        source_sig = source_fingerprint(
            title,
            story_text,
            summary,
            rows,
            config,
            model,
            target_duration,
            prompts,
            role_durations,
        )

        if verify_existing(source_sig, target_duration, timeline):
            previous = read_json(MANIFEST)
            write_json(CHECKPOINT, {
                "status": "completed",
                "version": VERSION,
                "source_fingerprint": source_sig,
                "sha256": previous.get("sha256"),
                "reused": True,
                "api_calls": 0,
                "paid_services_used": False,
                "role_composition_count": len(
                    previous.get("role_compositions", {})
                ),
                "role_segment_count": len(
                    previous.get("role_segments", [])
                ),
                "updated_at": time.time(),
            })
            return 0

        write_json(CHECKPOINT, {
            "status": "in_progress",
            "version": VERSION,
            "source_fingerprint": source_sig,
            "target_duration_seconds": target_duration,
            "narration_records": len(rows),
            "role_summary": role_summary,
            "role_durations": role_durations,
            "completed_role_compositions": [],
            "api_calls_started": 0,
            "updated_at": time.time(),
        })

        log("=" * 60)
        log("GOOGLE LYRIA — INDEPENDENT ROLE COMPOSITIONS")
        log("MODEL:", model)
        log("STORY:", title)
        log("PARTS:", len(role_summary["parts"]))
        log("SCENES RANGE:", (
            role_summary["scenes_min"],
            role_summary["scenes_max"],
        ))
        log("ACTUAL SCENES PER PART:", role_summary["actual_scene_counts"])
        log("NARRATION RECORDS:", len(rows))
        log("ROLE DURATIONS:", role_durations)
        log("REQUIRED MASTER DURATION:", target_duration)
        log("=" * 60)

        compositions = build_role_compositions(
            api_key,
            model,
            prompts,
            role_durations,
            role_rows,
        )

        expected_compositions = {
            role for role in SEGMENT_ROLES
            if role_durations[role] > 0
        }
        if set(compositions) != expected_compositions:
            raise RuntimeError(
                "Independent role composition coverage mismatch: "
                f"expected {sorted(expected_compositions)}, "
                f"found {sorted(compositions)}."
            )

        write_json(CHECKPOINT, {
            "status": "in_progress",
            "version": VERSION,
            "source_fingerprint": source_sig,
            "target_duration_seconds": target_duration,
            "completed_role_compositions": sorted(compositions),
            "role_compositions": compositions,
            "api_calls": API_CALLS,
            "paid_services_used": API_CALLS > 0,
            "updated_at": time.time(),
        })

        role_segments, role_segment_summary = build_role_segments(
            compositions,
            timeline,
        )

        narration_by_id = {
            row["scene_id"]: row for row in rows
        }
        verify_segment_records(
            role_segments,
            timeline,
            narration_by_id,
        )

        required_roles = build_role_counts(role_summary)
        actual_roles = {
            role: sum(
                1 for item in role_segments if item["role"] == role
            )
            for role in SEGMENT_ROLES
        }
        if actual_roles != required_roles:
            raise RuntimeError(
                "Role music segment counts do not match narration: "
                f"expected {required_roles}, found {actual_roles}."
            )

        master = build_master_from_segments(
            role_segments,
            target_duration,
        )
        validate_master_loudness(master["loudness"])

        final_checksum = sha256_file(FINAL_AUDIO)
        source_checksum = fingerprint({
            role: item["source_sha256"]
            for role, item in compositions.items()
        })

        record = {
            "status": "completed",
            "provider": "google_lyria",
            "product": "Google Lyria",
            "model": model,
            "generation_method": "four_independent_role_compositions",
            "source_audio": {
                role: item["source_path"]
                for role, item in compositions.items()
            },
            "path": FINAL_AUDIO.relative_to(ROOT).as_posix(),
            "audio_path": FINAL_AUDIO.relative_to(ROOT).as_posix(),
            "duration_seconds": round(master["duration_seconds"], 3),
            "actual_audio_duration_seconds": round(
                master["duration_seconds"], 3
            ),
            "required_duration_seconds": target_duration,
            "sha256": final_checksum,
            "source_sha256": source_checksum,
            "source_fingerprint": source_sig,
            "instrumental_only": True,
            "lyrics_requested": False,
            "narration_duration_source": (
                "sum_of_ffprobe_measured_tts_audio_files"
            ),
            "narration_records": len(rows),
            "role_summary": role_summary,
            "role_compositions": compositions,
            "role_composition_count": len(compositions),
            "role_composition_policy": (
                "Independent Google Lyria compositions are generated "
                "separately for Hook, Main, Suspense and Resolution. "
                "Each narration segment is extracted only from its "
                "matching role composition."
            ),
            "role_segments": role_segments,
            "role_segment_summary": role_segment_summary,
            "role_segment_directory": (
                SEGMENT_DIR.relative_to(ROOT).as_posix()
            ),
            "role_segment_policy": (
                "Every Hook, Main, Suspense and Resolution narration "
                "record receives a verified segment from its own "
                "independently generated role composition."
            ),
            "role_segment_timeline": timeline,
            "actual_scene_counts": role_summary["actual_scene_counts"],
            "scenes_min": role_summary["scenes_min"],
            "scenes_max": role_summary["scenes_max"],
            "story_aware_prompt": True,
            "story_beginning_considered": True,
            "story_middle_considered": True,
            "story_ending_considered": True,
            "common_story_identity_across_independent_roles": True,
            "narration_headroom": {
                "target_integrated_lufs": -25.0,
                "true_peak_limit_dbtp": -3.0,
                "measured": master["loudness"],
                "speech_midrange_preserved": True,
                "final_ducking_applied_by": "render_video.py",
            },
            "master_loudness": master["loudness"],
            "segment_loudness": {
                item["scene_id"]: item["loudness"]
                for item in role_segments
            },
            "timeline": timeline,
            "external_api_calls": API_CALLS,
            "paid_services_used": API_CALLS > 0,
            "size_bytes": master["size_bytes"],
            "version": VERSION,
            "completed_at": time.time(),
        }

        write_json(MANIFEST, record)
        published = validate_published_manifest(
            source_sig,
            final_checksum,
            target_duration,
            timeline,
        )

        write_json(CHECKPOINT, {
            "status": "completed",
            "version": VERSION,
            "source_fingerprint": source_sig,
            "sha256": final_checksum,
            "duration_seconds": round(master["duration_seconds"], 3),
            "target_duration_seconds": target_duration,
            "role_composition_count": len(
                published["role_compositions"]
            ),
            "role_composition_sha256": {
                role: item["sha256"]
                for role, item in published["role_compositions"].items()
            },
            "role_segment_count": len(published["role_segments"]),
            "role_segment_sha256": {
                item["scene_id"]: item["sha256"]
                for item in published["role_segments"]
            },
            "api_calls": API_CALLS,
            "paid_services_used": API_CALLS > 0,
            "updated_at": time.time(),
        })

        log("=" * 60)
        log("LYRIA MUSIC READY.")
        log("MODEL:", model)
        log("INDEPENDENT COMPOSITIONS:", sorted(compositions))
        log("ROLE SEGMENTS:", len(role_segments))
        log("ROLE COUNTS:", actual_roles)
        log("FINAL DURATION:", round(master["duration_seconds"], 2))
        log("INTEGRATED LOUDNESS:", master["loudness"]["integrated_lufs"])
        log("TRUE PEAK:", master["loudness"].get("true_peak_dbtp"))
        log("API CALLS:", API_CALLS)
        log("OUTPUT:", FINAL_AUDIO.relative_to(ROOT))
        log("MANIFEST:", MANIFEST.relative_to(ROOT))
        log("=" * 60)
        return 0

    except KeyboardInterrupt:
        write_json(CHECKPOINT, {
            "status": "interrupted",
            "version": VERSION,
            "source_fingerprint": source_sig,
            "api_calls": API_CALLS,
            "updated_at": time.time(),
        })
        raise

    except Exception as exc:
        write_unavailable(
            reason=exc,
            model=model,
            duration=target_duration,
            signature=source_sig,
        )
        log("LYRIA MUSIC UNAVAILABLE.")
        log("LOCAL SOUND-DESIGN FALLBACK MAY BE USED.")
        log("API CALLS:", API_CALLS)
        log("REASON:", str(exc)[:1500])
        log("NO FALSE COMPLETION RECORDED.")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Cancelled.")
        raise SystemExit(130)