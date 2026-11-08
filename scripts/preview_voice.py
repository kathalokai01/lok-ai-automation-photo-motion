#!/usr/bin/env python3
"""Create an optional, user-invoked voice preview without running the pipeline."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="auto", choices=[
        "auto", "child_friendly", "female_storyteller",
        "male_storyteller", "character",
    ])
    parser.add_argument("--generate", action="store_true",
                        help="Generate a short preview using Edge TTS; no generation occurs without this flag.")
    parser.add_argument("--text", default="नमस्कार! आइए, आज एक नई और रोचक कहानी सुनते हैं।")
    args = parser.parse_args()

    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from input_config import load_and_validate
    from voice_profiles import resolve_voice_profile

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    if args.profile != "auto":
        config["TTS_PROFILE"] = args.profile
    profile = resolve_voice_profile(config, {"role": "main", "emotion": "curiosity"})
    print(json.dumps(profile, ensure_ascii=False, indent=2))

    if not args.generate:
        print("Preview metadata only. Add --generate to create an Edge TTS sample.")
        return 0

    executable = shutil.which("edge-tts")
    if not executable:
        raise RuntimeError("edge-tts executable is unavailable.")
    profile_id = profile["profile_id"]
    if profile_id == "child_friendly":
        voice = "hi-IN-SwaraNeural"
    elif profile_id == "female_storyteller":
        voice = "hi-IN-SwaraNeural"
    else:
        voice = "hi-IN-MadhurNeural"
    output = ROOT / "output" / "voice_preview" / f"{profile_id}.mp3"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".partial.mp3")
    try:
        subprocess.run([
            executable, "--voice", voice, "--rate", "+0%",
            "--text", args.text, "--write-media", str(temporary),
        ], check=True, timeout=180, capture_output=True, text=True)
        if not temporary.is_file() or temporary.stat().st_size < 1000:
            raise RuntimeError("Edge TTS preview is empty or too small.")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print("PREVIEW:", output.relative_to(ROOT).as_posix())
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
