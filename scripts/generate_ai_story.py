#!/usr/bin/env python3
"""Generate story-grounded AI narration with configured LLM provider fallback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from input_config import (
    load_input_config,
    normalize_format,
    get_max_retries,
    get_topic,
    get_story_text,
)
from select_gemini_model import (
    configured_fallbacks, load_story_model_cursor, save_story_model_cursor,
)
from story_quality_validator import validate_story_part, validate_story_output
)