#!/usr/bin/env python3
"""Prepare validated, resumable photo-motion image jobs with character continuity."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes

INPUT_FILE = ROOT / "Input" / "topic.txt"
SCENES_FILE = ROOT / "output" / "scenes" / "scenes.json"
BIBLE_FILE = ROOT / "output" / "story" / "character_bible.json"
TITLE_FILE = ROOT / "output" / "story" / "final_title.txt"
VISUAL_DIR = ROOT / "output" / "visuals"
JOBS_FILE = VISUAL_DIR / "visual_jobs.json"
MANIFEST_FILE = VISUAL_DIR / "image_manifest.json"

VERSION = "photo-motion-13-character-gender-natural-contrast"
IMAGE_WIDTH = 1280
IMAGE_HEIGHT = 720
IMAGE_DIMENSIONS = (IMAGE_WIDTH, IMAGE_HEIGHT)
IMAGE_SIZE = f"{IMAGE_WIDTH}x{IMAGE_HEIGHT}"
MIN_IMAGE_BYTES = 5000

QUALITY = (
    "Photorealistic live-action cinematic photography. Realistic Indian "
    "people, locations, clothing and architecture. Natural skin texture, "
    "believable anatomy, realistic eyes, natural lighting and shadows. "
    "Maintain recurring character identity, age, facial features, hairstyle, "
    "body proportions and clothing continuity. Balanced exposure, natural "
    "skin tones, controlled highlights, visible shadow detail, moderate "
    "contrast, realistic cinematic color grading. No cartoon, anime, "
    "illustration, CGI, text, logo or watermark."
)

NEGATIVE_PROMPT = (
    "cartoon, anime, illustration, painting, CGI, plastic skin, wax figure, "
    "distorted face, bad anatomy, extra limbs, duplicated people, blurry face, "
    "unrealistic eyes, text, subtitles, watermark, logo, collage, split screen, "
    "poster lettering, floating objects, inconsistent character identity, "
    "unrelated characters, wrong era, unrealistic clothing, oversaturated "
    "skin, extreme contrast, crushed blacks, blown highlights, harsh HDR, "
    "unnatural skin color, excessive sharpening, impossible perspective, "
    "gender-inconsistent appearance, random costume changes"
)

DESCRIPTION_KEYS = (
    "visual_prompt",
    "image_prompt",
    "visual_description",
    "scene_description",
    "description",
    "prompt",
    "action",
    "text",
)

DURATION_KEYS = ("duration", "scene_duration", "duration_seconds", "seconds")

CATEGORY_KEYWORDS = {
    "horror": (
        "भूत", "प्रेत", "डरावनी", "खौफ", "चुड़ैल", "आत्मा", "हवेली",
        "शैतान", "horror", "ghost", "haunted", "witch", "demon", "scary",
        "supernatural",
    ),
    "mystery": (
        "रहस्य", "गुमशुदा", "गायब", "पहेली", "अनजान", "अज्ञात", "खोज",
        "mystery", "secret", "unknown", "disappearance", "investigation",
    ),
    "devotional": (
        "भक्ति", "भगवान", "देवी", "देवता", "मंदिर", "पूजा", "कृष्ण", "राम",
        "शिव", "विष्णु", "हनुमान", "दुर्गा", "गणेश", "पुराण", "अवतार",
        "divine", "devotional", "temple", "krishna", "rama", "shiva",
        "goddess", "mythology",
    ),
    "historical": (
        "राजा", "रानी", "सम्राट", "महाराज", "युद्ध", "किला", "इतिहास",
        "योद्धा", "साम्राज्य", "historical", "king", "queen", "emperor",
        "warrior", "battle", "kingdom",
    ),
    "emotional": (
        "माँ", "मां", "पिता", "परिवार", "बेटा", "बेटी", "प्यार", "प्रेम",
        "बलिदान", "दर्द", "emotional", "family", "mother", "father", "love",
        "sacrifice", "ईमान", "गरीबी", "honesty", "poverty",
    ),
    "nature": (
        "जंगल", "वन", "पहाड़", "नदी", "प्रकृति", "गाँव", "गांव", "किसान",
        "forest", "nature", "mountain", "river", "village", "farmer",
        "wildlife",
    ),
}

MALE_WORDS = (
    "male", "man", "boy", "father", "son", "brother", "husband",
    "grandfather", "पुरुष", "आदमी", "लड़का", "लड़के", "पिता", "बेटा",
    "भाई", "पति", "दादा", "नाना", "बूढ़ा", "युवक", "राजा", "किसान",
)

FEMALE_WORDS = (
    "female", "woman", "girl", "mother", "daughter", "sister", "wife",
    "grandmother", "महिला", "औरत", "लड़की", "लड़कियां", "माँ", "मां",
    "बेटी", "बहन", "पत्नी", "दादी", "नानी", "बूढ़ी", "युवती", "रानी",
)

MALE_ONLY_CLOTHING = (
    "saree", "sari", "साड़ी", "सिंदूर", "mangalsutra", "मंगलसूत्र",
    "women's blouse", "महिला ब्लाउज",
)

FEMALE_ONLY_FEATURES = (
    "beard", "moustache", "mustache", "दाढ़ी", "मूँछ", "मूंछ",
)

def log(*values):
    print(*values, flush=True)


def clean(value, limit=2000):
    if isinstance(value, (dict, list)):
        return ""
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def fingerprint(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sha256_text(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path, required=True):
    path = Path(path)

    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
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
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def first_story_line(value):
    for line in str(value or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^[A-Z_][A-Z0-9_]*\s*=", line):
            continue
        return clean(line, 250)
    return ""


def saved_title():
    if not TITLE_FILE.is_file():
        return ""

    try:
        for line in TITLE_FILE.read_text(
            encoding="utf-8-sig"
        ).splitlines():
            if clean(line):
                return clean(line, 200)
    except OSError as exc:
        log("WARNING: Saved title unavailable:", exc)

    return ""


def resolve_topic(config):
    topic = clean(config.get("TOPIC", ""), 250)
    if topic:
        return topic

    story_text = str(config.get("STORY_TEXT", "")).strip()
    if story_text:
        return saved_title() or first_story_line(story_text) or "Indian story"

    return saved_title() or "Indian cinematic story"


def resolve_title(topic, config):
    explicit = clean(config.get("TOPIC", ""), 250)
    if explicit:
        return explicit

    return saved_title() or topic


def detect_category(text):
    normalized = str(text or "").casefold()

    for category, words in CATEGORY_KEYWORDS.items():
        if any(word.casefold() in normalized for word in words):
            return category

    return "general"


def category_direction(category):
    directions = {
        "horror": (
            "Use believable low light and restrained shadows. Preserve details "
            "in dark areas. Do not invent monsters or events."
        ),
        "mystery": (
            "Use grounded visual clues and suspense. Do not invent plot events "
            "absent from the scene."
        ),
        "devotional": (
            "Use respectful devotional imagery only when called for. Do not "
            "invent deities, miracles or religious symbols."
        ),
        "historical": (
            "Use period-appropriate architecture, fabrics, tools and materials. "
            "Avoid anachronistic objects."
        ),
        "emotional": (
            "Emphasize believable expressions, natural skin tones and body language."
        ),
        "nature": (
            "Use realistic landscapes, vegetation, weather and natural colors."
        ),
        "general": (
            "Match lighting, color, composition and setting to the actual scene."
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
            raise RuntimeError("scenes.json must contain a scenes list.")
    else:
        raise RuntimeError("scenes.json must be an object or list.")

    if not scenes:
        raise RuntimeError("scenes.json contains no scenes.")

    if not all(isinstance(item, dict) for item in scenes):
        raise RuntimeError("Every scene must be a JSON object.")

    return scenes


def get_text(item, keys):
    for key in keys:
        value = item.get(key)
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

    value = positive_number(config.get("SCENE_DURATION"))
    if value is not None and 0.5 <= value <= 3600:
        return round(value, 3)

    return 5.0


def load_character_bible():
    data = read_json(BIBLE_FILE, required=False)

    if not isinstance(data, dict):
        log("NOTICE: Character Bible unavailable; continuing without it.")
        return {}

    return data


def scene_mapping(scene, index, seen):
    part = scene.get("part", 1)
    local = scene.get("scene", scene.get("local_scene", index))

    if isinstance(part, (dict, list)) or isinstance(local, (dict, list)):
        raise RuntimeError(f"Scene {index}: invalid part/scene identifiers.")

    try:
        part, local = int(part), int(local)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Scene {index}: part and scene must be integers."
        ) from exc

    if part < 1 or local < 1:
        raise RuntimeError(f"Scene {index}: identifiers must be positive.")

    pair = (part, local)
    if pair in seen:
        raise RuntimeError(f"Duplicate scene mapping: {pair}")

    seen.add(pair)
    return pair


def validate_order(mappings):
    previous = (0, 0)

    for index, current in enumerate(mappings, 1):
        if current <= previous:
            raise RuntimeError(
                f"Scene order is not part-then-scene at global scene {index}: "
                f"{current}. Fix scenes.json before generating images."
            )
        previous = current


def validate_expected_mapping(mappings, parts, scenes_per_part):
    if parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must both be positive.")

    expected = [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes_per_part + 1)
    ]

    if mappings != expected:
        actual_set = set(mappings)
        expected_set = set(expected)
        missing = sorted(expected_set - actual_set)
        extra = sorted(actual_set - expected_set)

        raise RuntimeError(
            "Scene mapping does not match Input/topic.txt. "
            f"Expected {len(expected)} scenes, found {len(mappings)}. "
            f"Missing={missing[:20]}; extra={extra[:20]}. "
            "Fix story/scene generation before creating images."
        )


def values_as_strings(value):
    """Normalize a scene character field that may be a string or list."""
    if isinstance(value, str):
        return [clean(value, 120)] if clean(value) else []

    if isinstance(value, list):
        return [
            clean(item, 120)
            for item in value
            if isinstance(item, str) and clean(item)
        ]

    if isinstance(value, dict):
        name = clean(value.get("name") or value.get("character_name"), 120)
        return [name] if name else []

    return []


def scene_explicit_names(scene):
    names = set()

    for key in ("character_name", "character", "characters", "cast"):
        for name in values_as_strings(scene.get(key)):
            names.add(name.casefold())

    return names


def scene_text_for_matching(scene):
    parts = []

    for key in (
        "title", "narration", "dialogue", "visual_prompt", "image_prompt",
        "visual", "description", "action", "character_name", "location",
        "setting",
    ):
        value = scene.get(key)

        if isinstance(value, str) and value.strip():
            parts.append(value.strip())

        elif isinstance(value, list):
            parts.extend(
                item.strip() for item in value
                if isinstance(item, str) and item.strip()
            )

    return " ".join(parts).casefold()


def character_matches_scene(character, scene_text, explicit_names):
    name = clean(character.get("name"), 120)

    if not name:
        return False

    normalized_name = name.casefold()

    if normalized_name in explicit_names:
        return True

    # Use a word boundary for Latin names to avoid accidental substring matches.
    if re.fullmatch(r"[a-zA-Z][a-zA-Z .'-]*", name):
        return bool(re.search(
            r"(?<!\w)" + re.escape(normalized_name) + r"(?!\w)",
            scene_text,
        ))

    # For non-Latin names, require the exact normalized name to appear.
    return normalized_name in scene_text


def normalize_gender(character):
    """Return only explicitly supported male/female labels; otherwise unknown."""
    raw = clean(
        character.get("gender")
        or character.get("sex")
        or character.get("gender_identity"),
        80,
    ).casefold()

    if raw in {"male", "man", "boy", "पुरुष", "आदमी", "लड़का"}:
        return "male"

    if raw in {"female", "woman", "girl", "महिला", "औरत", "लड़की"}:
        return "female"

    return "unknown"


def gender_prompt_rules(gender):
    if gender == "male":
        return (
            "GENDER CONTINUITY: Depict this named character as male, matching "
            "the established age and face. Do not add feminine-coded clothing "
            "or adornments such as a sari, sindoor, mangalsutra or women's blouse "
            "unless the scene explicitly requires that disguise, costume or event. "
            "Do not assume every man must have a beard or moustache; follow the "
            "character's established facial hair."
        )

    if gender == "female":
        return (
            "GENDER CONTINUITY: Depict this named character as female, matching "
            "the established age and face. Do not add a beard or moustache unless "
            "the scene explicitly describes a disguise or such an appearance. "
            "Do not force a sari, sindoor or jewelry if the scene specifies other "
            "clothing; preserve the established character design."
        )

    return (
        "GENDER CONTINUITY: Gender is not established clearly for this character. "
        "Do not infer gender from an ambiguous pronoun or name alone. Follow "
        "explicit scene descriptions and established visual references only."
    )


def collect_scene_context(scene, bible):
    """Select scene-relevant characters and retain their supported traits."""
    scene_text = scene_text_for_matching(scene)
    explicit_names = scene_explicit_names(scene)

    characters = bible.get("characters", [])
    selected = []

    if isinstance(characters, list):
        for character in characters:
            if not isinstance(character, dict):
                continue

            if not character_matches_scene(character, scene_text, explicit_names):
                continue

            name = clean(character.get("name"), 120)
            gender = normalize_gender(character)
            details = []

            for key in (
                "role", "age", "gender", "appearance", "face_features",
                "skin_tone", "hair", "eyes", "body_features", "clothing",
                "distinctive_features", "visual_identity", "continuity_notes",
            ):
                value = clean(character.get(key), 240)

                if not value:
                    continue

                if value.casefold().startswith((
                    "not specified", "not inferred", "do not invent",
                    "unknown", "not established",
                )):
                    continue

                details.append(f"{key.replace('_', ' ')}: {value}")

            source_details = character.get("source_details", [])
            if isinstance(source_details, list):
                for detail in source_details[:4]:
                    detail = clean(detail, 240)
                    if detail and detail not in details:
                        details.append(detail)

            selected.append({
                "name": name,
                "gender": gender,
                "details": details[:10],
                "gender_rule": gender_prompt_rules(gender),
            })

    # Preserve explicitly described scene details.
    scene_details = []

    for key, label in (
        ("character_name", "character"),
        ("age", "age"),
        ("gender", "gender"),
        ("appearance", "appearance"),
        ("clothing", "clothing"),
        ("location", "location"),
        ("setting", "setting"),
        ("time_period", "time period"),
        ("weather", "weather"),
        ("time_of_day", "time of day"),
    ):
        value = scene.get(key)

        if isinstance(value, str) and value.strip():
            scene_details.append(f"{label}: {clean(value, 300)}")

    # Include list-form character names without pretending they define appearance.
    for key in ("characters", "cast"):
        for name in values_as_strings(scene.get(key)):
            if name.casefold() not in explicit_names:
                continue
            marker = f"named character: {name}"
            if marker not in scene_details:
                scene_details.append(marker)

    world = bible.get("world", {})
    world_details = []

    if isinstance(world, dict):
        for key in ("setting", "time_period", "geography"):
            value = clean(world.get(key), 350)
            if value and not value.casefold().startswith(
                ("not explicitly specified", "unknown", "not specified")
            ):
                world_details.append(f"{key.replace('_', ' ')}: {value}")

    return selected[:8], scene_details[:10], world_details[:3]


def build_prompt(
    mode,
    dimensions,
    topic,
    title,
    category,
    part,
    local_scene,
    description,
    scene,
    bible,
):
    framing = (
        "Horizontal 16:9 cinematic composition, 1280x720. Use a clear visual "
        "subject, natural foreground and background depth, believable camera "
        "perspective, and realistic proportions. Keep faces and important action "
        "away from extreme edges. No vertical framing, collage or split screen."
    )

    characters, scene_details, world_details = collect_scene_context(scene, bible)
    continuity = []

    for character in characters:
        detail_text = "; ".join(character["details"])
        line = f"- {character['name']}"

        if detail_text:
            line += f": {detail_text}"

        line += f"\n  {character['gender_rule']}"
        continuity.append(line)

    sections = []

    if continuity:
        sections.append(
            "RELEVANT CHARACTER CONTINUITY — keep each named character's identity "
            "consistent across scenes:\n" + "\n".join(continuity)
        )

    if scene_details:
        sections.append(
            "EXPLICIT SCENE DETAILS:\n- " + "\n- ".join(scene_details)
        )

    if world_details:
        sections.append(
            "WORLD CONTINUITY:\n- " + "\n- ".join(world_details)
        )

    if sections:
        sections.append(
            "Apply only details supported by the story. Explicit scene details "
            "override general character-bible notes when the scene clearly "
            "describes a deliberate change. Do not invent unsupported gender, "
            "age, facial hair, jewelry, clothing, physical traits or plot events. "
            "For a returning character, preserve face shape, skin tone, age, "
            "hairstyle, body proportions and established clothing unless the "
            "story describes a change."
        )

    continuity_text = "\n\n".join(sections)

    return (
        "Create one original photorealistic live-action cinematic photograph "
        "for an Indian story video.\n"
        f"STORY TOPIC: {topic}\n"
        f"STORY TITLE: {title}\n"
        f"GENRE: {category}\n"
        f"GENRE DIRECTION: {category_direction(category)}\n"
        f"FORMAT: {mode}; target dimensions: {dimensions}.\n"
        f"PART: {part}; LOCAL SCENE: {local_scene}.\n"
        f"SCENE TO DEPICT: {description}\n\n"
        "VISUAL QUALITY AND EXPOSURE RULES:\n"
        "- Natural, believable Indian skin tones; preserve realistic skin texture.\n"
        "- Balanced exposure and moderate cinematic contrast.\n"
        "- Keep useful detail in shadows; avoid crushed blacks and blown highlights.\n"
        "- Use soft, motivated lighting and realistic shadow transitions.\n"
        "- Avoid excessive saturation, neon color casts, artificial HDR and harsh "
        "edge sharpening.\n"
        "- Dark or suspenseful scenes may be dim, but faces and important objects "
        "must remain naturally visible when the scene lighting allows.\n"
        "- Bright scenes must retain detail in faces, clothing and the sky.\n\n"
        "Depict the actual characters, action, location, time, weather and emotion. "
        "Use believable expressions, realistic anatomy, natural lighting and "
        "culturally appropriate Indian details when relevant. Create one coherent "
        "photograph, not a collage, split screen, poster or slideshow. Do not add "
        "unrelated people, objects, costumes, locations or plot events. No "
        "lettering, captions, logos or watermarks.\n"
        f"{framing}\n\n{continuity_text}"
    )


def image_prompt_hash(prompt, negative_prompt):
    # Keep compatibility with free_image_generator.py make_prompt().
    combined = (
        f"{prompt}\n\nQuality: {QUALITY}\n\nAvoid: {negative_prompt}"
    )
    return sha256_text(combined)


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
            except (TypeError, ValueError):
                continue

            if number > 0:
                result[number] = row

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
            return (
                image.size == dimensions
                and image.format in ("PNG", "JPEG", "WEBP")
            )

    except Exception:
        return False


def main():
    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = load_and_validate(INPUT_FILE)
    topic = resolve_topic(config)
    title = resolve_title(topic, config)

    mode = str(get_format(config)).strip().lower()
    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported video format: {mode}")

    parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must both be positive.")

    dimensions = IMAGE_SIZE
    width, height = IMAGE_DIMENSIONS

    scenes = extract_scenes(read_json(SCENES_FILE))
    bible = load_character_bible()

    description_text = " ".join(
        get_text(scene, DESCRIPTION_KEYS) for scene in scenes
    )
    category = detect_category(f"{topic} {title} {description_text}")

    mappings = []
    seen_pairs = set()

    for index, scene in enumerate(scenes, 1):
        mappings.append(scene_mapping(scene, index, seen_pairs))

    validate_order(mappings)
    validate_expected_mapping(mappings, parts, scenes_per_part)

    VISUAL_DIR.mkdir(parents=True, exist_ok=True)
    previous = previous_manifest_rows()
    jobs = []
    manifest_rows = []

    for index, scene in enumerate(scenes, 1):
        part, local_scene = mappings[index - 1]
        description = get_text(scene, DESCRIPTION_KEYS)

        if not description:
            raise RuntimeError(
                f"Scene {index} has no visual prompt. "
                "Refusing to generate an unrelated image."
            )

        duration = scene_duration(scene, config)
        image_path = f"output/visuals/scene_{index:04d}.png"

        prompt = build_prompt(
            mode,
            dimensions,
            topic,
            title,
            category,
            part,
            local_scene,
            description,
            scene,
            bible,
        )

        prompt_hash = image_prompt_hash(prompt, NEGATIVE_PROMPT)
        scene_id = clean(
            scene.get(
                "scene_id",
                f"part_{part:02d}_scene_{local_scene:02d}",
            ),
            150,
        )

        job = {
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
        }
        jobs.append(job)

        destination = ROOT / image_path
        old = previous.get(index, {})
        matching_prompt = old.get("prompt_sha256") == prompt_hash
        image_ok = valid_existing_image(destination, IMAGE_DIMENSIONS)

        prior_hash = str(old.get("image_sha256", "")).strip()
        checksum_matches = (
            not prior_hash
            or (image_ok and sha256_file(destination) == prior_hash)
        )

        old_status = str(old.get("status", ""))

        if matching_prompt and image_ok and checksum_matches:
            status = old_status

            if status not in {
                "generated", "existing", "reused", "completed"
            }:
                status = "existing"

            provider = old.get("provider") or "previous run"
            image_hash = sha256_file(destination)
        else:
            status = "image_required"
            provider = None
            image_hash = None

        manifest_rows.append({
            "scene": index,
            "global_scene": index,
            "global_scene_number": index,
            "scene_id": scene_id,
            "part": part,
            "local_scene": local_scene,
            "image": image_path,
            "image_path": image_path,
            "image_sha256": image_hash,
            "width": width,
            "height": height,
            "duration": duration,
            "status": status,
            "provider": provider,
            "prompt_sha256": prompt_hash,
        })

    if len(jobs) != parts * scenes_per_part:
        raise RuntimeError("Image-job count does not match requested scene count.")

    if len(jobs) != len(manifest_rows):
        raise RuntimeError("Image jobs and manifest counts do not match.")

    ready = sum(
        row["status"] in {"generated", "existing", "reused", "completed"}
        for row in manifest_rows
    )

    source_fingerprint = fingerprint({
        "version": VERSION,
        "topic": topic,
        "title": title,
        "category": category,
        "format": mode,
        "dimensions": dimensions,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
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

    metadata = {
        "version": VERSION,
        "source_fingerprint": source_fingerprint,
        "format": mode,
        "story_topic": topic,
        "story_title": title,
        "story_category": category,
        "image_size": dimensions,
        "width": width,
        "height": height,
        "parts": parts,
        "scenes_per_part": scenes_per_part,
        "total_scenes": len(jobs),
        "motion_engine": "photo_motion",
        "uses_wan2gp": False,
        "uses_colab": False,
    }

    write_json(JOBS_FILE, {**metadata, "jobs": jobs})
    write_json(MANIFEST_FILE, {
        **metadata,
        "images_ready": ready,
        "images_required": len(manifest_rows),
        "scenes": manifest_rows,
    })

    log("=" * 56)
    log("IMAGE JOB PREPARATION COMPLETED")
    log("Topic:", topic)
    log("Title:", title)
    log("Category:", category)
    log("Format:", mode)
    log("Dimensions:", dimensions)
    log("Parts:", parts)
    log("Scenes per part:", scenes_per_part)
    log("Scenes:", len(jobs))
    log("Reusable valid images:", ready)
    log("Images requiring generation:", len(jobs) - ready)
    log("Jobs:", JOBS_FILE.relative_to(ROOT))
    log("Manifest:", MANIFEST_FILE.relative_to(ROOT))
    log("=" * 56)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Image-job preparation cancelled.")
        sys.exit(130)
    except Exception as exc:
        log(f"IMAGE JOB PREPARATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)