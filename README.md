# Katha Lok AI — Photo Motion Automation

यह repository generated still images को **2D/2.5D Photo Motion** में बदलती है: camera zoom, pan और depth-map parallax. यह character/body/face animation project नहीं है.

## Output modes

| Setting | Output | Resolution |
|---|---|---:|
| `FORMAT = "short"` | केवल vertical Shorts | 720 × 1280 |
| `FORMAT = "full"` | केवल landscape full-length video | 1280 × 720 |

- एक run में केवल चुना हुआ FORMAT बनता है; opposite-format copy या combined video नहीं बनता.
- SHORT mode में चुने गए हर Part की अलग MP4 file बनती है.
- FULL mode हमेशा एक Part बनाता है.
- FPS का default 24 है; `Input/topic.txt` में configured FPS 60 से अधिक नहीं हो सकता.

## Input configuration

सभी user settings `Input/topic.txt` में रखें. कम-से-कम `TOPIC`, `TITLE` या `STORY_TEXT` में से एक दें.

### Story

```ini
FORMAT = "short"
AUDIENCE = "adult"

TITLE = ""
TOPIC = "अपनी कहानी का विषय"
PRESERVE_STORY_TEXT = true
STORY_TEXT = ""

PARTS = "4-6"
SCENES = "7-15"
```

- यदि पूरी कहानी `STORY_TEXT` में दी गई है, तो उसे मूल स्रोत माना जाता है. वर्तनी, मात्रा, व्याकरण और विराम-चिह्न सुधारे जा सकते हैं; कहानी का अर्थ, पात्र, घटनाएँ, क्रम और अंत नहीं बदलना है.
- यदि पूरी कहानी नहीं दी गई है, तो `TOPIC` या `TITLE` के आधार पर कहानी बनाई जा सकती है.
- खाली `TITLE` होने पर शीर्षक पूरी कहानी के अर्थ से निकाला जाता है.

### PARTS: fixed count या inclusive range

उदाहरण:

```ini
PARTS = 4
PARTS = "2-8"
PARTS = "4-6"
PARTS = "7-11"
PARTS = "12-18"
```

- एक संख्या fixed Part count है.
- `A-B` inclusive range है; चुना हुआ Part count corrected story की word count के अनुसार range के भीतर तय होता है.
- उलटी range, जैसे `11-8`, invalid है.
- SHORT mode में हर Part का लक्ष्य 120–150 शब्द है; 150 से अधिक होने पर retry होता है और 180 तक स्वीकार्य सीमा है. 120 से कम अंतिम Part को पहले rebalance किया जाता है; जरूरत पर उसे पिछले Part में merge किया जा सकता है, जिसमें अंतिम merged Part की सीमा 239 शब्द तक हो सकती है.
- FULL mode में PARTS हमेशा 1 होता है; दिए गए दूसरे PARTS मान को लागू नहीं किया जाता.
- Range selection persist होती है ताकि बाद के workflow steps में Part count एक जैसा रहे.

### SCENES: प्रत्येक Part की अलग-अलग scene count range

```ini
SCENES = "7-15"
# या fixed count:
SCENES = 9
```

- दोनों सीमाएँ inclusive हैं.
- हर Part का actual Main Scene count range में होना चाहिए; सभी Parts में समान count जरूरी नहीं है.
- Scene boundaries पूर्ण वाक्य और अर्थपूर्ण clauses पर आधारित होती हैं. वाक्य के बीच में कहानी नहीं काटी जाती.
- वैकल्पिक `SCENES_MIN` और `SCENES_MAX` देने पर वे `SCENES` range से मेल खाने चाहिए.
- उलटी range invalid है. Maximum 500 Scenes प्रति Part है और configured maximum total Main Scenes 1,000 से अधिक नहीं हो सकता.

## Workflow

GitHub repository में **Actions → Generate Katha Lok AI Photo Motion Video → Run workflow** चुनें. Workflow Python 3.11 environment तैयार करता है और क्रम से:

1. Configuration तथा provider/model selection जाँचता है.
2. कहानी बनाता/सुधारता है, Part count और story structure verify करता है.
3. Story plan, character bible, scenes और narration records बनाता है.
4. Caption source और measured TTS narration audio तैयार करता है.
5. Background music और visual-generation jobs तैयार करता है.
6. Scene images generate/reuse करके image manifests verify करता है.
7. Depth maps बनाता है और Photo Motion scene clips render करता है.
8. Music, ambience और SFX timing validate करता है.
9. Scene clips और Part MP4 render करता है; captions हर role clip और Part video पर burn होते हैं.
10. Story-specific thumbnail और YouTube metadata बनाता है.
11. Final media, scene completeness, audio, captions, dimensions और manifests validate करता है.
12. Story-named ZIP बनाकर ZIP CRC, member hashes और embedded manifest verify करता है.

## Output files

- Part videos: `output/parts/part_XX.mp4`
- Scene Photo Motion clips: `output/photo_motion/`
- Scene images and depth maps: `output/visuals/images/` और `output/visuals/depth_maps/`
- Scene narration audio: `output/narration/audio/`
- Flow Music manifest and role-aligned source/segment audio: `output/music/flow_music_manifest.json`, `output/music/role_sources/`, `output/music/role_segments/`
- Captions: `output/captions/`
- Thumbnail: `output/thumbnail.jpg`
- Validation report: `output/validation_report.json`
- Package report: `output/package_manifest.json`
- ZIP filename: `output/package_archive_name.txt` में दर्ज story-specific नाम

ZIP के अंदर story-slug और mode वाला root folder होता है. इसमें सभी Part MP4, सभी rendered Scene के source images और narration audio, depth maps, sound-design/SFX audio assets, चुना हुआ Hindi caption font और उसका OFL license, captions, thumbnail, metadata और जरूरी story/visual/audio project manifests शामिल होते हैं. Duplicate, unreferenced media, temporary files और combined/opposite-format videos package में स्वीकार नहीं होते.

## Reuse और provider policy

- Valid story, image, audio, depth, scene-render और thumbnail checkpoints fingerprint/hash validation के बाद reuse किए जाते हैं; stale या invalid artifacts ही दोबारा बनते हैं.
- Configured image और TTS provider order तथा fallback order को `Input/topic.txt` में जैसा रखा है वैसा ही रखें.
- Workflow LLM calls के लिए zero-cost-only policy लागू करता है. Billable image/TTS providers तभी उपयोग किए जा सकते हैं जब configuration और credentials उन्हें अनुमति दें.
- API keys GitHub Actions Secrets में रखें; उन्हें `Input/topic.txt`, source files या README में न लिखें.

## Dependencies

- Python 3.11.
- Python packages pinned in `requirements.txt`.
- CPU PyTorch is installed separately by the workflow from the official PyTorch CPU wheel index.
- System commands: `ffmpeg`, `ffprobe`, `fc-match`, `fc-scan`, `zip`, `unzip`.
- Hindi caption fonts are installed and verified by `scripts/install_caption_fonts.py`; see the font fallback section below.

The workflow and preflight verify required Python modules and system commands before the expensive generation stages. Use `python -m pip check` to detect incompatible installed package requirements.

## Hindi caption fonts

Font installation checks existing project/system fonts first, verifies font files and fontconfig resolution, copies the selected Hindi font to `output/fonts/selected_hindi_caption_font.ttf`, writes its SIL Open Font License 1.1 notice to `output/fonts/OFL-1.1.txt`, and records the selected fallback and SHA-256 in `output/fonts/font_manifest.json`.

Fallback order:

1. Noto Sans Devanagari
2. Mukta
3. Hind
4. Tiro Devanagari Hindi

The caption renderer resolves the configured family through fontconfig and fails clearly if no usable Hindi font is available. Do not hard-code a machine-specific font path.

## Local checks

From the repository root, after installing the system tools and Python dependencies:

```bash
python -m pip check
python scripts/install_caption_fonts.py
python scripts/preview_caption_fonts.py
python scripts/pipeline_preflight.py
```

The full workflow also requires the provider credentials/settings used by the selected configured provider order. A successful preflight is not the same as a completed video-generation run.
