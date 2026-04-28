"""Unit test untuk filters.py — semua offline, no API call."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import filters  # noqa: E402


# ----------------------------------------------------------------------
# A. Credits
# ----------------------------------------------------------------------

def test_credits_translated_by():
    inp = "Translated by Lucky7\nThis is the actual chapter content."
    out, stats = filters.clean_text(inp)
    assert "Translated by Lucky7" not in out
    assert "This is the actual chapter content." in out
    assert stats.removed_lines.get("credits", 0) >= 1


def test_credits_tl_prefix():
    cases = [
        "TL: Skyfarrow",
        "TL'd by SomeUser",
        "Translation: Anna",
        "Trans: Bob",
        "Translator: Owl",
        "Editor: Foo",
        "Edited by Bar",
        "Proofread by Baz",
        "PR: Qux",
        "Source: novelupdates.com",
        "Original Author: 田中",
        "Read more at lightnovelpub.com",
        "Read advanced chapters on patreon.com/foo",
    ]
    for line in cases:
        text = f"{line}\nReal content here."
        out, _ = filters.clean_text(text)
        assert line not in out, f"Should remove: {line}"
        assert "Real content here." in out


# ----------------------------------------------------------------------
# B. Donate
# ----------------------------------------------------------------------

def test_donate_patreon_kofi_paypal():
    cases = [
        "Support me on Patreon",
        "Become a patron at patreon.com/translator",
        "Buy me a Ko-fi: ko-fi.com/abc",
        "Buy Me a Coffee at buymeacoffee.com/xx",
        "PayPal: paypal.me/translator",
        "Donate via PayPal here.",
        "Tip jar: $1 helps",
        "Send a tip!",
        "Saweria.co/translator",
        "Trakteer.id/translator",
    ]
    for line in cases:
        text = f"Story content.\n{line}"
        out, _ = filters.clean_text(text)
        assert line not in out, f"Should remove: {line}"
        assert "Story content." in out


# ----------------------------------------------------------------------
# C. Social
# ----------------------------------------------------------------------

def test_social_discord_telegram_etc():
    cases = [
        "Join our Discord at discord.gg/abc",
        "Discord: discord.gg/xyz",
        "Telegram channel: t.me/foo",
        "Follow us on Twitter @abc",
        "Join us on Instagram",
        "Our YouTube channel",
    ]
    for line in cases:
        text = f"{line}\nNarasi."
        out, _ = filters.clean_text(text)
        assert line not in out, f"Should remove: {line}"
        assert "Narasi." in out


# ----------------------------------------------------------------------
# D. Navigation
# ----------------------------------------------------------------------

def test_navigation_prev_next_index():
    cases = [
        "<< Previous | Index | Next >>",
        "Previous Chapter | Next Chapter",
        "[Prev] [TOC] [Next]",
        "← Previous | Next →",
        "Table of Contents",
        "Back to Index",
        "Return to Main",
        "Chapter list",
    ]
    for line in cases:
        text = f"Real content paragraph.\n{line}"
        out, _ = filters.clean_text(text)
        assert line not in out, f"Should remove: {line}"
        assert "Real content paragraph." in out


def test_navigation_separators():
    cases = [
        "===",
        "----------",
        "*****",
        "~~~~",
        "______",
        "—————",
    ]
    for line in cases:
        text = f"Para 1.\n{line}\nPara 2."
        out, _ = filters.clean_text(text)
        assert line not in out, f"Should remove separator: {line!r}"
        assert "Para 1." in out
        assert "Para 2." in out


def test_navigation_to_be_continued():
    inp = "Last sentence of chapter.\nTo be continued..."
    out, _ = filters.clean_text(inp)
    assert "To be continued" not in out
    assert "Last sentence of chapter." in out


# ----------------------------------------------------------------------
# E. Ads / engagement
# ----------------------------------------------------------------------

def test_ads_engagement():
    cases = [
        "Like and comment if you enjoyed this chapter",
        "Don't forget to subscribe",
        "Hit the bell icon",
        "Smash that like button",
        "Leave a comment below",
        "Leave a review on NovelUpdates",
        "Rate this novel on NU",
        "Share this chapter with your friends",
        "Bookmark this site",
        "Add this to your reading list",
        "Vote for us on Novel Updates",
        "If you enjoyed this chapter, consider supporting.",
    ]
    for line in cases:
        text = f"Akhir cerita.\n{line}"
        out, _ = filters.clean_text(text)
        assert line not in out, f"Should remove: {line}"


# ----------------------------------------------------------------------
# F. Schedule / sponsorship
# ----------------------------------------------------------------------

def test_schedule_sponsored():
    cases = [
        "Sponsored chapter by Anonymous",
        "Thanks to John for sponsoring",
        "Bonus chapter for reaching 100 patrons",
        "Mass release this weekend",
        "This week's release schedule:",
        "Release schedule: Mon/Wed/Fri",
        "Chapter 5 of 10 this month",
        "Queue: 3 chapters",
        "Goal: 50 patrons for bonus",
    ]
    for line in cases:
        text = f"{line}\nIsi cerita."
        out, _ = filters.clean_text(text)
        assert line not in out, f"Should remove: {line}"
        assert "Isi cerita." in out


# ----------------------------------------------------------------------
# G. TL notes (default OFF, harus dipertahankan)
# ----------------------------------------------------------------------

def test_tl_notes_kept_by_default():
    inp = (
        "TL Note: 'aniki' means older brother.\n"
        "Author's note: thanks for reading\n"
        "[TN: this is a pun]\n"
        "Real content here."
    )
    out, _ = filters.clean_text(inp)
    assert "TL Note: 'aniki' means older brother." in out
    assert "Author's note: thanks for reading" in out
    assert "[TN: this is a pun]" in out
    assert "Real content here." in out


def test_tl_notes_removed_when_enabled():
    inp = (
        "TL Note: 'aniki' means older brother.\n"
        "Author's note: thanks for reading\n"
        "Real content here."
    )
    enabled = filters.DEFAULT_ENABLED + ("tl_notes",)
    out, stats = filters.clean_text(inp, enabled=enabled)
    assert "TL Note" not in out
    assert "Author's note" not in out
    assert "Real content here." in out
    assert stats.removed_lines.get("tl_notes", 0) >= 2


# ----------------------------------------------------------------------
# H. HTML residue
# ----------------------------------------------------------------------

def test_html_entity_replacement():
    inp = "Hello&nbsp;world. Salt &amp; pepper. Quote: &quot;hi&quot;."
    out, stats = filters.clean_text(inp)
    assert "&nbsp;" not in out
    assert "&amp;" not in out
    assert "&quot;" not in out
    assert "Hello world." in out
    assert "Salt & pepper." in out
    assert 'Quote: "hi".' in out
    assert stats.html_substitutions >= 4


def test_html_tags_stripped():
    inp = "<p>Paragraf 1.</p>\n<p><span class='x'>Paragraf 2</span> dengan <em>italic</em>.</p>"
    out, _ = filters.clean_text(inp)
    assert "<p>" not in out
    assert "</p>" not in out
    assert "<span" not in out
    assert "Paragraf 1." in out
    assert "Paragraf 2" in out
    # <em> -> *
    assert "*italic*" in out


def test_html_comments_removed():
    inp = "Real text. <!-- this is a comment --> More text."
    out, _ = filters.clean_text(inp)
    assert "<!--" not in out
    assert "-->" not in out
    assert "Real text." in out
    assert "More text." in out


# ----------------------------------------------------------------------
# I. Footer / watermark
# ----------------------------------------------------------------------

def test_footer_copyright():
    cases = [
        "All rights reserved.",
        "Copyright 2024 SomeTranslator",
        "© 2024 SomeName",
        "(c) 2025 Someone",
        "Do not repost without permission.",
        "Unauthorized translation is prohibited.",
        "For free reading only at lightnovelpub.com",
        "If you see this elsewhere, please report.",
        "If you are reading this on another site, please visit the original.",
        "This translation is hosted exclusively on royalroad.com",
        "Please support the original author.",
    ]
    for line in cases:
        text = f"Cerita.\n{line}"
        out, _ = filters.clean_text(text)
        assert line not in out, f"Should remove: {line}"
        assert "Cerita." in out


def test_footer_bare_url():
    inp = "Story sentence.\nhttps://example.com/translator/site\nMore story."
    out, _ = filters.clean_text(inp)
    assert "https://example.com/translator/site" not in out
    assert "Story sentence." in out
    assert "More story." in out


# ----------------------------------------------------------------------
# J. Whitespace normalization
# ----------------------------------------------------------------------

def test_whitespace_multiple_blank_lines():
    inp = "Para 1.\n\n\n\n\nPara 2.\n\n\nPara 3."
    out, _ = filters.clean_text(inp)
    # Setelah normalisasi, max 2 newlines berturut-turut
    assert "\n\n\n" not in out
    assert "Para 1." in out
    assert "Para 2." in out
    assert "Para 3." in out


def test_whitespace_trailing_spaces():
    inp = "Para 1.   \nPara 2.\t\nPara 3."
    out, _ = filters.clean_text(inp)
    for line in out.split("\n"):
        assert line == line.rstrip()


# ----------------------------------------------------------------------
# False positives — JANGAN hapus narasi normal
# ----------------------------------------------------------------------

def test_no_false_positive_normal_narrative():
    real_lines = [
        "Aku berjalan ke arahnya dengan hati-hati.",
        "Dia menatapku dengan mata penuh harap.",
        '"Apa kabar?" tanyaku pelan.',
        "Hujan turun perlahan-lahan di luar jendela.",
        "Bab 1: Awal dari Segalanya",  # judul chapter normal
        "Sarah tersenyum tipis.",
        "Aku tidak tahu apa yang harus kukatakan.",
    ]
    inp = "\n".join(real_lines)
    out, stats = filters.clean_text(inp)
    for line in real_lines:
        assert line in out, f"FALSE POSITIVE: removed real line: {line}"
    assert stats.removed_lines.get("credits", 0) == 0
    assert stats.removed_lines.get("ads", 0) == 0


def test_no_false_positive_words_with_substrings():
    # Kata yang mengandung "patron", "subscribe", dll dalam konteks valid
    real_lines = [
        "Dia melihat orang itu sebagai patron seninya.",   # 'patron' tapi narasi
        "Mereka menulis di papan tulis dengan kapur.",
        "Anda bukan lawanku.",  # baris dialog formal — JANGAN dihapus
        "Aku akan mengikuti perintahmu.",
    ]
    inp = "\n".join(real_lines)
    out, _ = filters.clean_text(inp)
    for line in real_lines:
        assert line in out, f"FALSE POSITIVE: removed real line: {line}"


# ----------------------------------------------------------------------
# Custom patterns
# ----------------------------------------------------------------------

def test_custom_patterns_via_inline_list():
    inp = "Site: foo.com\nReal content.\nDelete this specific line."
    out, _ = filters.clean_text(
        inp,
        custom_patterns=[r"^\s*delete\s+this.*$"],
    )
    assert "Delete this specific line." not in out
    assert "Real content." in out


def test_custom_patterns_invalid_regex_silently_skipped():
    # Invalid regex jangan crash, cuma di-skip
    inp = "Real content."
    out, _ = filters.clean_text(inp, custom_patterns=["[invalid("])
    assert "Real content." in out


# ----------------------------------------------------------------------
# FilterEngine.from_config
# ----------------------------------------------------------------------

def test_from_config_default_when_empty():
    eng = filters.FilterEngine.from_config({})
    # Default enabled: tidak include tl_notes
    assert "credits" in eng.enabled
    assert "tl_notes" not in eng.enabled


def test_from_config_disabled_globally():
    eng = filters.FilterEngine.from_config({"enabled": False})
    assert eng.enabled == ()
    # Even with credits in input, nothing removed
    inp = "Translated by Foo\nReal content."
    out, _ = eng.clean(inp)
    assert "Translated by Foo" in out


def test_from_config_specific_categories_off():
    eng = filters.FilterEngine.from_config({
        "enabled": True,
        "categories": {"credits": False, "donate": False},
    })
    assert "credits" not in eng.enabled
    assert "donate" not in eng.enabled
    assert "navigation" in eng.enabled


def test_from_config_per_novel_filters_file(tmp_path: Path):
    f = tmp_path / "filters.txt"
    f.write_text(
        "# komentar\n"
        "\n"
        "cleanup-this-line.*\n"
        "another-pattern\s+oops\n",
        encoding="utf-8",
    )
    eng = filters.FilterEngine.from_config({}, custom_patterns_path=f)
    inp = "cleanup-this-line yes\nReal content.\nanother-pattern oops"
    out, _ = eng.clean(inp)
    assert "cleanup-this-line yes" not in out
    assert "another-pattern oops" not in out
    assert "Real content." in out


def test_custom_pattern_anchored_to_full_line_no_false_positive():
    # Regression: pattern "translated" should NOT match a narrative line
    # that happens to contain or start with the word.
    # Custom patterns are documented as full-line anchored, so they must
    # NOT do partial matches.
    inp = (
        "translated version of the original story was a masterpiece.\n"
        "Translated by Foo\n"   # this is real boilerplate (caught by built-in 'credits')
    )
    out, _ = filters.clean_text(inp, custom_patterns=[r"translated"])
    # Narrative line MUST be preserved despite using the bare custom pattern
    assert "translated version of the original story was a masterpiece." in out


def test_custom_pattern_full_line_match_works():
    # Sister regression: a bare token as full-line content IS removed.
    inp = "translated\nReal content.\n"
    out, _ = filters.clean_text(inp, custom_patterns=[r"translated"])
    assert "Real content." in out
    # The standalone "translated" line should be gone
    assert not any(line.strip() == "translated" for line in out.split("\n"))


# ----------------------------------------------------------------------
# Dry-run
# ----------------------------------------------------------------------

def test_dry_run_returns_lines_and_categories():
    inp = (
        "Translated by Foo\n"
        "Real para 1.\n"
        "Support me on Patreon\n"
        "Real para 2.\n"
        "Discord: discord.gg/x\n"
    )
    eng = filters.FilterEngine()
    matches = eng.dry_run(inp)
    cats_seen = {cat for _, _, cat in matches}
    assert "credits" in cats_seen
    assert "donate" in cats_seen
    assert "social" in cats_seen
    # Dry-run jangan ubah text asli
    out, _ = eng.clean(inp)
    assert "Real para 1." in out and "Real para 2." in out


# ----------------------------------------------------------------------
# NovelUpdates-specific
# ----------------------------------------------------------------------

def test_novelupdates_patterns():
    cases = [
        "Add this novel to your NU reading list",
        "Check out my other novels on NU",
        "Vote this novel on NovelUpdates",
    ]
    for line in cases:
        text = f"Cerita.\n{line}"
        out, _ = filters.clean_text(text)
        assert line not in out, f"Should remove: {line}"
        assert "Cerita." in out


# ----------------------------------------------------------------------
# Integration: realistic scraped chapter
# ----------------------------------------------------------------------

def test_integration_realistic_scraped_chapter():
    raw = """\
=== Chapter 1: The Beginning ===

Translated by Lucky7 | Edited by Owl
Source: lightnovelpub.com
Read advanced chapters at patreon.com/lucky7

<< Previous | Index | Next >>

I walked into the cafe and saw Sarah waving at me.

"Hey, you finally made it!" she said.

"Sorry I'm late," I replied.

The conversation flowed naturally between us.

<!-- ad placeholder -->
<p>If you enjoyed this chapter, please leave a comment.</p>
Join our Discord at discord.gg/lucky7

To be continued...

—————
© 2024 Lucky7 Translations. All rights reserved.
Do not repost without permission.
"""
    out, stats = filters.clean_text(raw)
    # Should be kept
    assert "Chapter 1: The Beginning" in out
    assert "I walked into the cafe and saw Sarah waving at me." in out
    assert '"Hey, you finally made it!" she said.' in out
    assert '"Sorry I\'m late," I replied.' in out
    assert "The conversation flowed naturally between us." in out
    # Should be removed
    assert "Translated by Lucky7" not in out
    assert "Source: lightnovelpub.com" not in out
    assert "patreon.com/lucky7" not in out
    assert "Previous | Index | Next" not in out
    assert "<!-- ad placeholder -->" not in out
    assert "<p>" not in out
    assert "leave a comment" not in out
    assert "discord.gg/lucky7" not in out
    assert "To be continued" not in out
    assert "All rights reserved" not in out
    assert "Do not repost" not in out

    # Statistik harus tercatat
    assert stats.total_removed() > 0


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            sig_args = []
            # Sederhana: kalau test menerima tmp_path, buat sendiri.
            import inspect
            params = inspect.signature(fn).parameters
            kwargs = {}
            if "tmp_path" in params:
                import tempfile
                kwargs["tmp_path"] = Path(tempfile.mkdtemp())
            fn(**kwargs)
            print(f"PASS  {fn.__name__}")
        except AssertionError:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
