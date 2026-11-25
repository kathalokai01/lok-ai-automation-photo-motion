from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import prepare_captions


def test_english_names_and_universal_terms_are_preserved():
    source = (
        "नया टावर्स इंजीनियर टेक सिटाडेल लॉन्च ओरियन बग सिटी चीफ "
        "मिलियन्स डॉलर कोड बायपास पार्टी क्रैश माँ हेलीकॉप्टर "
        "सर्वर रूम स्लैब व्हीलचेयर जेल"
    )
    result = prepare_captions.romanize_hindi_to_hinglish(source)
    expected = (
        "New towers engineer tech citadel launch Orion bug city chief "
        "millions dollar code bypass party crash Maa helicopter "
        "server room slab wheelchair jail"
    )
    assert result.casefold() == expected.casefold()


def test_existing_english_words_are_not_transliterated():
    source = "The new Orion server room has a bug in the code."
    assert prepare_captions.romanize_hindi_to_hinglish(source) == source


def test_loanword_mapping_does_not_replace_substrings_inside_other_hindi_words():
    source = "नयापन और इंजीनियरिंग"
    result = prepare_captions.romanize_hindi_to_hinglish(source)
    assert "new" not in result.casefold()
    assert "engineering" in result.casefold()


def test_explicit_hinglish_caption_is_romanized_if_it_contains_devanagari():
    row = {
        "caption_hinglish": "ओरियन ने नया server launch किया",
        "narration": "ओरियन ने नया server launch किया",
        "language": "hindi",
    }
    text, _, _ = prepare_captions.choose_caption(row, "hinglish")
    assert "Orion" in text
    assert "new" in text
    assert "server" in text
    assert "launch" in text
    assert not any("\u0900" <= ch <= "\u097f" for ch in text)
