#!/usr/bin/env python3
"""Story-aware, validated and resumable cinematic image-job preparation."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_title_source

INPUT_FILE = ROOT / "Input" / "topic.txt"
SCENES_FILE = ROOT / "output" / "scenes" / "scenes.json"
BIBLE_FILE = ROOT / "output" / "story" / "character_bible.json"
TITLE_FILE = ROOT / "output" / "story" / "final_title.txt"
VISUAL_DIR = ROOT / "output" / "visuals"
JOBS_FILE = VISUAL_DIR / "visual_jobs.json"
MANIFEST_FILE = VISUAL_DIR / "image_manifest.json"

VERSION = "photo-motion-9.0"
MIN_IMAGE_BYTES = 5000

QUALITY = (
    "Photorealistic live-action cinematic photography. Realistic Indian "
    "people, clothing, locations and architecture. Natural skin texture, "
    "believable anatomy, realistic eyes, physically plausible lighting, "
    "natural shadows, detailed real-world materials and cinematic depth. "
    "Keep recurring character identity, age, hairstyle, skin tone, build "
    "and clothing consistent. No cartoon, anime, illustration, CGI, text, "
    "logo, watermark, collage or split screen."
)

NEGATIVE_PROMPT = (
    "cartoon, anime, illustration, painting, CGI, plastic skin, wax figure, "
    "distorted face, bad anatomy, extra limbs, duplicated people, blurry "
    "face, unrealistic eyes, text, subtitles, watermark, logo, collage, "
    "split screen, poster lettering, floating objects, inconsistent "
    "character identity, unrelated characters, wrong era, unrealistic "
    "clothing, oversaturated skin, impossible perspective"
)

DESCRIPTION_KEYS = (
    "visual_prompt",
    "image_prompt",
    "visual_description",
    "description",
    "scene_description",
    "prompt",
    "action",
    "text",
)

DURATION_KEYS = ("duration", "scene_duration", "duration_seconds", "seconds")

CATEGORY_KEYWORDS = {
    "horror": (
        "भूत", "प्रेत", "डरावनी", "खौफ", "चुड़ैल", "आत्मा",
        "हवेली", "शैतान", "horror", "ghost", "haunted", "witch",
        "demon", "scary", "supernatural",
    ),
    "mystery": (
        "रहस्य", "गुमशुदा", "गायब", "पहेली", "अनजान", "अज्ञात",
        "खोज", "mystery", "secret", "unknown", "disappearance",
        "investigation",
    ),
    "devotional": (
        "भक्ति", "भगवान", "देवी", "देवता", "मंदिर", "पूजा",
        "कृष्ण", "राम", "शिव", "विष्णु", "हनुमान", "दुर्गा",
        "गणेश", "पुराण", "अवतार", "divine", "devotional",
        "temple", "krishna", "rama", "shiva", "goddess", "mythology",
    ),
    "historical": (
        "राजा", "रानी", "सम्राट", "महाराज", "युद्ध", "किला",
        "इतिहास", "योद्धा", "साम्राज्य", "historical", "king",
        "queen", "emperor", "warrior", "battle", "kingdom",
    ),
    "emotional": (
        "माँ", "मां", "पिता", "परिवार", "बेटा", "बेटी", "प्यार",
        "प्रेम", "बलिदान", "दर्द", "emotional", "family",
        "mother", "father", "love", "sacrifice", "ईमान", "गरीबी",
        "honesty", "poverty",
    ),
    "nature": (
        "जंगल", "वन", "पहाड़", "नदी", "प्रकृति", "गाँव", "गांव",
        "किसान", "forest", "nature", "mountain", "river",
        "village", "farmer", "wildlife",
    ),
}


def log(*items):
    print(*items, flush=True)


def fingerprint(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_json(path, required=True):
    path = Path(path)
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file is missing: {path}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def clean_text(value, limit=1000):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def read_title_file():
    if not TITLE_FILE.is_file():
        return ""

    try:
        for line in TITLE_FILE.read_text(encoding="utf-8-sig").splitlines():
            title = clean_text(line, 200)
            if title:
                return title
    except OSError as exc:
        log("WARNING: Could not read saved title:", exc)

    return ""


def first_story_line(story_text):
    for line in str(story_text or "").splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        if re.match(r"^[A-Z_][A-Z0-9_]*\s*=", line):
            continue

        cleaned = clean_text(line, 250)
        if cleaned:
            return cleaned

    return ""


def resolve_topic(config):
    explicit = clean_text(config.get("TOPIC", ""), 250)
    if explicit:
        return explicit

    story_text = str(config.get("STORY_TEXT", "")).strip()
    if story_text:
        first_line = first_story_line(story_text)
        if first_line:
            return first_line

        configured_title = clean_text(get_title_source(config), 250)
        if configured_title:
            return configured_title

    # A saved title is used only if the current input has no new story.
    saved_title = read_title_file()
    if saved_title:
        return saved_title

    return clean_text(get_title_source(config), 250) or "Indian cinematic story"


def resolve_title(topic, config):
    explicit = clean_text(config.get("TOPIC", ""), 250)
    story_text = str(config.get("STORY_TEXT", "")).strip()

    if explicit or story_text:
        return explicit or topic

    return read_title_file() or topic


def detect_category(text):
    normalized = str(text or "").casefold()

    for category in (
        "horror",
        "mystery",
        "devotional",
        "historical",
        "emotional",
        "nature",
    ):
        if any(
            word.casefold() in normalized
            for word in CATEGORY_KEYWORDS[category]
        ):
            return category

    return "general"


def category_direction(category):
    directions = {
        "devotional": (
            "Use respectful devotional visual language only when the scene "
            "calls for it. Do not invent deities, miracles or religious symbols."
        ),
        "horror": (
            "Use restrained shadows, believable low light and suspenseful "
            "framing. Do not invent monsters, gore or events absent from the scene."
        ),
        "mystery": (
            "Use cinematic suspense and visual clues grounded in the scene. "
            "Do not reveal or invent plot events that are not described."
        ),
        "historical": (
            "Use period-appropriate architecture, fabrics, tools and materials. "
            "Avoid modern objects in historical scenes."
        ),
        "emotional": (
            "Emphasize believable expressions, natural body language and human "
            "connection. Avoid exaggerated expressions or melodrama."
        ),
        "nature": (
            "Use realistic landscapes, vegetation, weather and materials "
            "consistent with the described location and season."
        ),
        "general": (
            "Choose lighting, colors and composition according to the actual "
            "scene, characters, setting, time and emotion."
        ),
    }

    return directions.get(category, directions["general"])


def extract_scenes(data):
    if isinstance(data, list):
        scenes = data
    elif isinstance(data, dict):
        scenes = next(
            (
                data[key]
                for key in ("scenes", "scene_list", "items", "data")
                if isinstance(data.get(key), list)
            ),
            None,
        )
        if scenes is None:
            raise RuntimeError(
                "scenes.json must contain scenes, scene_list, items or data."
            )
    else:
        raise RuntimeError("scenes.json must be a JSON object or list.")

    if not scenes:
        raise RuntimeError("scenes.json contains no scenes.")

    if any(not isinstance(scene, dict) for scene in scenes):
        raise RuntimeError("Every scene must be a JSON object.")

    return scenes


def get_text(scene, keys):
    for key in keys:
        value = scene.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def positive_number(value):
    try:
        number = float(value)
        if math.isfinite(number) and number > 0:
            return number
    except (TypeError, ValueError):
        pass

    return None


def scene_duration(scene, config):
    for key in DURATION_KEYS:
        value = positive_number(scene.get(key))
        if value is not None and 0.5 <= value <= 3600:
            return round(value, 3)

    configured = positive_number(config.get("SCENE_DURATION"))
    if configured is not None and 0.5 <= configured <= 3600:
        return round(configured, 3)

    return 5.0


def image_format_and_size(config):
    mode = str(get_format(config)).strip().lower()
    if mode == "full":
        return "full", "1280x720"

    return "short", "720x1280"


def load_character_bible():
    data = read_json(BIBLE_FILE, required=False)

    if data is None or data == {} or data == []:
        log("NOTICE: Character Bible unavailable.")
        return ""

    if not isinstance(data, (dict, list)):
        raise RuntimeError("character_bible.json must be an object or list.")

    return json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )[:12000]


def scene_mapping(scene, index, seen_pairs):
    part = scene.get("part", 1)
    local_scene = scene.get("scene", scene.get("local_scene", index))

    if isinstance(part, (dict, list)) or isinstance(local_scene, (dict, list)):
        raise RuntimeError(
            f"Scene {index}: part and scene identifiers must be scalar."
        )

    try:
        part_number = int(part)
        local_number = int(local_scene)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Scene {index}: part/scene identifiers must be integers."
        ) from exc

    if part_number < 1 or local_number < 1:
        raise RuntimeError(f"Scene {index}: invalid part/scene mapping.")

    pair = (part_number, local_number)

    if pair in seen_pairs:
        raise RuntimeError(f"Duplicate part/scene mapping: {pair}")

    seen_pairs.add(pair)
    return part_number, local_number


def validate_scene_order(mappings):
    """Require scene order to be chronological and stable across parts."""
    previous = (0, 0)

    for global_index, pair in enumerate(mappings, start=1):
        if pair <= previous:
            raise RuntimeError(
                "Scene mapping is not in part-then-scene order at global "
                f"scene {global_index}: {pair}. Fix scenes.json before "
                "generating images to avoid mismatched narration and video."
            )

        previous = pair


def build_prompt(
    mode,
    dimensions,
    topic,
    title,
    category,
    part,
    local_scene,
    description,
    bible,
):
    if mode == "short":
        framing = (
            "Vertical 9:16 mobile-video composition. Keep faces and important "
            "action away from extreme edges. Compose for a portrait crop with "
            "clear foreground, subject and background depth."
        )
    else:
        framing = (
            "Horizontal 16:9 cinematic composition with a clear focal subject "
            "and balanced foreground, middle-ground and background."
        )

    continuity = ""
    if bible:
        continuity = (
            "\nCHARACTER CONTINUITY: Apply only to characters actually present. "
            "Preserve face, age, hairstyle, skin tone, build and clothing. "
            "Do not put every character in every image. The scene description "
            "takes priority when deciding who appears.\nCHARACTER BIBLE: "
            + bible
        )

    return (
        "Create one original photorealistic live-action cinematic image for "
        "an Indian story video.\n"
        f"STORY TOPIC: {topic}\n"
        f"STORY TITLE: {title}\n"
        f"GENRE HINT: {category}\n"
        f"GENRE VISUAL DIRECTION: {category_direction(category)}\n"
        f"FORMAT: {mode}; target dimensions: {dimensions}.\n"
        f"PART: {part}; LOCAL SCENE: {local_scene}.\n"
        f"SCENE TO DEPICT: {description}\n"
        "The scene description is the source of truth. Depict its actual "
        "characters, action, location, time, weather and emotion. Use "
        "believable expressions, realistic lighting and culturally appropriate "
        "Indian details when relevant. Create one coherent photograph, not a "
        "collage, split screen, poster or slideshow. Do not invent unrelated "
        "people, objects, costumes, locations or plot events. Do not include "
        "title lettering, captions, logos or watermarks. "
        f"{framing}{continuity}"
    )


def prompt_fingerprint(prompt, negative_prompt):
    complete_prompt = (
        f"{prompt}\n\nQuality: {QUALITY}\n\nAvoid: {negative_prompt}"
    )
    return hashlib.sha256(complete_prompt.encode("utf-8")).hexdigest()


def previous_manifest_rows():
    data = read_json(MANIFEST_FILE, required=False)
    rows = data.get("scenes", []) if isinstance(data, dict) else data
    result = {}

    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue

            try:
                number = int(row.get("global_scene", row.get("scene")))
                if number > 0:
                    result[number] = row
            except (TypeError, ValueError):
                continue

    return result


def valid_existing_image(path, dimensions):
    try:
        from PIL import Image

        path = Path(path)
        if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
            return False

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            if image.size != dimensions:
                return False
            if image.format not in ("PNG", "JPEG", "WEBP"):
                return False

        return True
    except Exception:
        return False


def main():
    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = load_and_validate(INPUT_FILE)
    topic = resolve_topic(config)
    title = resolve_title(topic, config)
    mode, dimensions = image_format_and_size(config)
    width, height = map(int, dimensions.split("x"))

    scenes = extract_scenes(read_json(SCENES_FILE))
    bible = load_character_bible()

    # Include actual scene descriptions in genre detection. A generic topic
    # should not override a clearly described horror, historical or nature scene.
    description_text = " ".join(
        get_text(scene, DESCRIPTION_KEYS) for scene in scenes
    )
    category = detect_category(
        topic + " " + title + " " + description_text
    )

    previous = previous_manifest_rows()
    VISUAL_DIR.mkdir(parents=True, exist_ok=True)

    jobs = []
    manifest_rows = []
    seen_pairs = set()
    mappings = []

    for index, scene in enumerate(scenes, start=1):
        part, local_scene = scene_mapping(scene, index, seen_pairs)
        mappings.append((part, local_scene))

    validate_scene_order(mappings)

    for index, scene in enumerate(scenes, start=1):
        part, local_scene = mappings[index - 1]
        description = get_text(scene, DESCRIPTION_KEYS)

        if not description:
            raise RuntimeError(
                f"Scene {index} has no visual description. Fix scenes.json "
                "instead of generating an unrelated image."
            )

        duration = scene_duration(scene, config)
        image_path = f"output/visuals/scene_{index:02d}.png"

        prompt = build_prompt(
            mode,
            dimensions,
            topic,
            title,
            category,
            part,
            local_scene,
            description,
            bible,
        )
        prompt_hash = prompt_fingerprint(prompt, NEGATIVE_PROMPT)

        scene_id = str(
            scene.get(
                "scene_id",
                f"part_{part:02d}_scene_{local_scene:02d}",
            )
        )

        jobs.append({
            "scene": index,
            "global_scene": index,
            "global_scene_number": index,
            "scene_id": scene_id,
            "part": part,
            "local_scene": local_scene,
            "status": "image_required",
            "image_path": image_path,
            "prompt": prompt,
            "prompt_sha256": prompt_hash,
            "negative_prompt": NEGATIVE_PROMPT,
            "image_size": dimensions,
            "duration": duration,
            "motion_engine": "photo_motion",
            "output_video": f"output/photo_motion/scene_{index:04d}.mp4",
        })

        destination = ROOT / image_path
        old = previous.get(index, {})

        prompt_matches = old.get("prompt_sha256") == prompt_hash
        image_valid = valid_existing_image(destination, (width, height))
        old_status = str(old.get("status", ""))

        if (
            prompt_matches
            and image_valid
            and old_status not in ("failed", "image_required", "pending")
        ):
            status = old_status
            if status not in ("generated", "existing", "reused", "completed"):
                status = "existing"

            provider = old.get("provider") or "previous run"
        else:
            status = "image_required"
            provider = None

        manifest_rows.append({
            "scene": index,
            "global_scene": index,
            "global_scene_number": index,
            "scene_id": scene_id,
            "part": part,
            "local_scene": local_scene,
            "image": image_path,
            "image_path": image_path,
            "duration": duration,
            "status": status,
            "provider": provider,
            "prompt_sha256": prompt_hash,
        })

    if len(jobs) != len(manifest_rows):
        raise RuntimeError("Image jobs and manifest counts do not match.")

    ready = sum(
        row["status"] in ("generated", "existing", "reused", "completed")
        for row in manifest_rows
    )

    source_fingerprint = fingerprint({
        "version": VERSION,
        "topic": topic,
        "title": title,
        "category": category,
        "format": mode,
        "dimensions": dimensions,
        "character_bible": bible,
        "scenes": [
            {
                "part": job["part"],
                "local_scene": job["local_scene"],
                "scene_id": job["scene_id"],
                "prompt_sha256": job["prompt_sha256"],
                "duration": job["duration"],
            }
            for job in jobs
        ],
    })

    jobs_document = {
        "version": VERSION,
        "source_fingerprint": source_fingerprint,
        "format": mode,
        "story_topic": topic,
        "story_title": title,
        "story_category": category,
        "image_size": dimensions,
        "total_scenes": len(jobs),
        "motion_engine": "photo_motion",
        "uses_wan2gp": False,
        "uses_colab": False,
        "jobs": jobs,
    }

    manifest_document = {
        "version": VERSION,
        "source_fingerprint": source_fingerprint,
        "format": mode,
        "story_topic": topic,
        "story_title": title,
        "story_category": category,
        "total_scenes": len(manifest_rows),
        "images_ready": ready,
        "images_required": len(manifest_rows),
        "scenes": manifest_rows,
    }

    write_json(JOBS_FILE, jobs_document)
    write_json(MANIFEST_FILE, manifest_document)

    log("=" * 60)
    log("STORY-AWARE PHOTO-MOTION IMAGE JOBS PREPARED")
    log("Topic:", topic)
    log("Title:", title)
    log("Visual category:", category)
    log("Format:", mode)
    log("Image dimensions:", dimensions)
    log("Scenes:", len(jobs))
    log("Valid matching images retained:", ready)
    log("Images requiring generation:", len(jobs) - ready)
    log("Source fingerprint:", source_fingerprint[:16])
    log("Character Bible:", "available" if bible else "not available")
    log("Jobs:", JOBS_FILE.relative_to(ROOT))
    log("Manifest:", MANIFEST_FILE.relative_to(ROOT))
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("VISUAL JOB PREPARATION CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"VISUAL JOB PREPARATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)