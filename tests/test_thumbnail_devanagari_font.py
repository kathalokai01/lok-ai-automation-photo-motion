from pathlib import Path
import sys
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_thumbnail
import install_caption_fonts


def test_font_coverage_requires_devanagari_codepoints(tmp_path):
    font = tmp_path / "candidate.ttf"
    font.write_bytes(b"font-placeholder")
    result = SimpleNamespace(stdout="20-7e 900-97f", returncode=0)
    with patch.object(generate_thumbnail.subprocess, "run", return_value=result):
        assert generate_thumbnail.font_has_devanagari(font)


def test_font_coverage_rejects_latin_only_font(tmp_path):
    font = tmp_path / "candidate.ttf"
    font.write_bytes(b"font-placeholder")
    result = SimpleNamespace(stdout="20-7e a0-ff", returncode=0)
    with patch.object(generate_thumbnail.subprocess, "run", return_value=result):
        assert not generate_thumbnail.font_has_devanagari(font)


def test_thumbnail_font_candidates_exclude_latin_only_fallbacks():
    candidates = " ".join(generate_thumbnail.FONT_CANDIDATES).casefold()
    assert "dejavusans" not in candidates
    assert "liberation" not in candidates

def test_font_installer_requires_devanagari_coverage(tmp_path):
    font = tmp_path / "candidate.ttf"
    font.write_bytes(b"font-placeholder")
    result = SimpleNamespace(stdout="20-7e 900-97f", returncode=0)
    with patch.object(install_caption_fonts, "run", return_value=result):
        assert install_caption_fonts.has_devanagari_coverage(font)


def test_font_installer_rejects_latin_only_font(tmp_path):
    font = tmp_path / "candidate.ttf"
    font.write_bytes(b"font-placeholder")
    result = SimpleNamespace(stdout="20-7e a0-ff", returncode=0)
    with patch.object(install_caption_fonts, "run", return_value=result):
        assert not install_caption_fonts.has_devanagari_coverage(font)

