"""Smoke tests for translate.py (offline, tanpa Gemini)."""
from __future__ import annotations

import io
import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

# Allow `import translate` ketika dijalankan dari root atau tests/
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import translate  # noqa: E402


# ----------------------------------------------------------------------
# detect_language
# ----------------------------------------------------------------------

def test_detect_english():
    assert translate.detect_language("Hello world. This is a normal English sentence.") == "en"


def test_detect_japanese_with_kana():
    text = "俺は教室に入った。お前、本気か?「ドキドキ」"
    assert translate.detect_language(text) == "jp"


def test_detect_korean_hangul():
    text = "나는 교실에 들어갔다. 너 정말 괜찮아?"
    assert translate.detect_language(text) == "kr"


def test_detect_chinese_hanzi_no_kana():
    text = "我走进了房间。你真的没事吗?师父让我去办一件事。" * 3
    assert translate.detect_language(text) == "cn"


def test_detect_short_unknown_falls_back_to_en():
    assert translate.detect_language("") == "en"


# ----------------------------------------------------------------------
# normalize_pronouns: safe mode
# ----------------------------------------------------------------------

def test_normalize_safe_replaces_casual_anda():
    inp = "Apakah Anda baik-baik saja?"
    out = translate.normalize_pronouns(inp, strength="safe")
    assert out == "Apakah kamu baik-baik saja?"


def test_normalize_safe_replaces_casual_saya():
    inp = "Saya akan datang besok. Tunggu saya, ya."
    out = translate.normalize_pronouns(inp, strength="safe")
    assert out == "Aku akan datang besok. Tunggu aku, ya."


def test_normalize_safe_preserves_formal_paragraph_yang_mulia():
    inp = "Yang Mulia, Saya mohon ampun. Anda tidak perlu khawatir."
    out = translate.normalize_pronouns(inp, strength="safe")
    # Penanda "Yang Mulia" -> paragraf di-skip
    assert out == inp


def test_normalize_safe_preserves_formal_paragraph_tuan():
    inp = "Tuan Smith, Anda dipanggil oleh atasan."
    out = translate.normalize_pronouns(inp, strength="safe")
    assert out == inp


def test_normalize_safe_preserves_formal_paragraph_shifu():
    inp = "Shifu, Saya akan mengikuti perintah Anda."
    out = translate.normalize_pronouns(inp, strength="safe")
    assert out == inp


def test_normalize_safe_does_not_touch_andaikan():
    # 'andaikan' jangan dipenggal jadi 'kamuikan'
    inp = "Andaikan saja Anda tahu, andai itu mungkin."
    out = translate.normalize_pronouns(inp, strength="safe")
    assert "Andaikan" in out
    assert "andai" in out
    assert "kamu tahu" in out


def test_normalize_safe_does_not_touch_sayur_or_sayang():
    inp = "Saya beli sayur. Sayang sekali Anda tidak ikut."
    out = translate.normalize_pronouns(inp, strength="safe")
    assert "sayur" in out
    assert "Sayang" in out
    assert "Aku beli" in out
    assert "kamu tidak" in out


def test_normalize_safe_works_per_paragraph():
    inp = (
        "Saya pulang sekolah dan bertemu Anda di taman.\n\n"
        "Yang Mulia, Saya mohon ampun atas kesalahan Anda."
    )
    out = translate.normalize_pronouns(inp, strength="safe")
    paragraphs = out.split("\n\n")
    assert paragraphs[0] == "Aku pulang sekolah dan bertemu kamu di taman."
    # Paragraf kedua skip karena ada "Yang Mulia"
    assert paragraphs[1] == "Yang Mulia, Saya mohon ampun atas kesalahan Anda."


# ----------------------------------------------------------------------
# normalize_pronouns: aggressive
# ----------------------------------------------------------------------

def test_normalize_aggressive_replaces_even_in_formal():
    inp = "Yang Mulia, Saya mohon ampun. Anda tidak perlu khawatir."
    out = translate.normalize_pronouns(inp, strength="aggressive")
    assert out == "Yang Mulia, Aku mohon ampun. kamu tidak perlu khawatir."


# ----------------------------------------------------------------------
# normalize_pronouns: off
# ----------------------------------------------------------------------

def test_normalize_off_returns_unchanged():
    inp = "Saya dan Anda."
    assert translate.normalize_pronouns(inp, strength="off") == inp
    assert translate.normalize_pronouns(inp, strength="") == inp


# ----------------------------------------------------------------------
# load_prompt_template
# ----------------------------------------------------------------------

def test_load_prompt_templates_for_all_supported_langs():
    for lang in translate.SUPPORTED_LANGS:
        tpl = translate.load_prompt_template(lang)
        # Setiap template harus punya placeholder yang dipakai build_translation_prompt
        assert "{glossary_block}" in tpl
        assert "{text}" in tpl
        # Dan harus mengandung instruksi default kasual yang ketat
        assert "aku" in tpl.lower() and "kamu" in tpl.lower()


def test_load_prompt_unknown_lang_falls_back_to_en():
    tpl_unknown = translate.load_prompt_template("xx")
    tpl_en = translate.load_prompt_template("en")
    assert tpl_unknown == tpl_en


# ----------------------------------------------------------------------
# build_translation_prompt: integrasi sederhana
# ----------------------------------------------------------------------

def test_build_translation_prompt_substitutes():
    template = translate.load_prompt_template("en")
    glossary = translate.Glossary(
        characters={"John": "John"},
        terms={"Mana": "Mana"},
    )
    prompt = translate.build_translation_prompt(template, "Hello, John.", glossary)
    assert "{glossary_block}" not in prompt
    assert "{text}" not in prompt
    assert "Hello, John." in prompt
    assert "Mana" in prompt


def _run_dry_run_filter(novel_subdir: str, chapter_text: str):
    """Helper: jalankan cmd_translate dengan --dry-run-filter di sandbox.

    Buat NOVELS_DIR sementara, hapus GEMINI_API_KEY, dan capture state
    filesystem & spy SEBELUM cleanup supaya caller bisa assert dengan akurat.

    Returns dict berisi:
      - rc: return code dari cmd_translate
      - glossary_existed: True kalau glossary.json tercipta selama
        cmd_translate jalan (bukti glossary auto-build kepicu).
      - gemini_client_called: True kalau GeminiClient() di-instantiate
        (bukti API path ke-reach).
    """
    import argparse
    import os
    import shutil
    import tempfile

    # Sandbox: NOVELS_DIR di tempfile + API key kosong.
    tmpdir = Path(tempfile.mkdtemp(prefix="dryrun_"))
    orig_novels_dir = translate.NOVELS_DIR
    orig_api_key = os.environ.pop("GEMINI_API_KEY", None)
    orig_client = translate.GeminiClient

    # Spy: track kalau GeminiClient di-instantiate. Kalau ya, dry-run path
    # tidak benar-benar lokal (bug yang kita coba prevent).
    spy: dict = {"called": False}

    class _SpyGeminiClient:
        def __init__(self, *args, **kwargs):
            spy["called"] = True
            raise RuntimeError("GeminiClient should NOT be instantiated during --dry-run-filter")

    translate.NOVELS_DIR = tmpdir
    translate.GeminiClient = _SpyGeminiClient  # type: ignore[misc]

    try:
        novel_dir = tmpdir / novel_subdir
        src = novel_dir / "source"
        src.mkdir(parents=True)
        (src / "Chapter_001.txt").write_text(chapter_text, encoding="utf-8")

        args = argparse.Namespace(
            novel=novel_subdir,
            lang=None,
            only=None,
            rebuild=False,
            build_glossary=False,
            dry_run_filter=True,
            list=False,
            cmd=None,
        )
        rc = translate.cmd_translate(args)

        # Snapshot filesystem state SEBELUM cleanup. Kalau dicek caller
        # setelah cleanup, novel_dir sudah dihapus jadi .exists() selalu False
        # (assertion vacuously true).
        glossary_existed = (novel_dir / "glossary.json").exists()

        return {
            "rc": rc,
            "glossary_existed": glossary_existed,
            "gemini_client_called": spy["called"],
        }
    finally:
        translate.NOVELS_DIR = orig_novels_dir
        translate.GeminiClient = orig_client  # type: ignore[misc]
        if orig_api_key is not None:
            os.environ["GEMINI_API_KEY"] = orig_api_key
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_dry_run_filter_works_without_api_key():
    """Regression: --dry-run-filter must not require GEMINI_API_KEY and must
    not instantiate GeminiClient (purely local operation)."""
    result = _run_dry_run_filter(
        "_dryrun_test",
        "Translated by Foo\nReal narrative here.\n",
    )
    # Jangan crash dengan RuntimeError ("API key belum diset"), harus return 0.
    assert result["rc"] == 0
    # GeminiClient TIDAK boleh di-instantiate sama sekali untuk dry-run.
    assert result["gemini_client_called"] is False


def test_dry_run_filter_does_not_trigger_glossary_auto_build():
    """Regression: --dry-run-filter must not trigger glossary auto-build
    even when glossary is empty and mode='auto' (default)."""
    result = _run_dry_run_filter(
        "_dryrun_glossary_test",
        "Patreon: patreon.com/foo\nThe story begins.\n",
    )
    assert result["rc"] == 0
    # Glossary.json TIDAK boleh tercipta (bukti auto-build tidak terpicu).
    assert result["glossary_existed"] is False
    # Dan GeminiClient (yang di-init sebelum auto-build di kode buggy) juga
    # tidak boleh dipanggil.
    assert result["gemini_client_called"] is False


# ----------------------------------------------------------------------
# Engine selector & RuneriaClient
# ----------------------------------------------------------------------

def _silent_logger() -> logging.Logger:
    lg = logging.getLogger("test")
    lg.handlers.clear()
    lg.addHandler(logging.NullHandler())
    return lg


def test_engine_unknown_raises():
    cfg = {"engine": "totallyfake"}
    try:
        translate.create_llm_client(cfg, _silent_logger())
    except ValueError as e:
        assert "totallyfake" in str(e)
        return
    raise AssertionError("Expected ValueError untuk engine tidak dikenal")


def test_engine_runeria_factory_picks_runeria():
    cfg = {"engine": "runeria", "runeria": {"api_key": "fake-key-for-test"}}
    client = translate.create_llm_client(cfg, _silent_logger())
    assert isinstance(client, translate.RuneriaClient)
    assert client.translate_model_name == "claude-sonnet-4"


def test_engine_runeria_requires_api_key():
    """Tanpa api_key di config & tanpa env var, harus error jelas."""
    cfg = {"engine": "runeria", "runeria": {"api_key": ""}}
    orig = os.environ.pop("RUNERIA_API_KEY", None)
    try:
        try:
            translate.create_llm_client(cfg, _silent_logger())
        except RuntimeError as e:
            assert "RUNERIA_API_KEY" in str(e)
            return
        raise AssertionError("Expected RuntimeError tanpa API key")
    finally:
        if orig is not None:
            os.environ["RUNERIA_API_KEY"] = orig


def test_engine_cli_override_beats_config():
    """--engine runeria override engine: gemini di config."""
    cfg = {"engine": "gemini", "runeria": {"api_key": "fake"}}
    client = translate.create_llm_client(cfg, _silent_logger(), engine_override="runeria")
    assert isinstance(client, translate.RuneriaClient)


def test_runeria_generate_parses_openai_response():
    """Mock urlopen, pastikan generate() return content dari choices[0].message.content."""
    cfg = {"engine": "runeria", "runeria": {
        "api_key": "fake", "requests_per_minute": 0,
    }}
    client = translate.create_llm_client(cfg, _silent_logger())

    captured = {}

    class _FakeResp:
        def __init__(self, payload: dict):
            self._buf = json.dumps(payload).encode("utf-8")
        def read(self): return self._buf
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp({
            "choices": [{
                "message": {"content": "Halo dunia."},
                "finish_reason": "stop",
            }],
        })

    with patch("urllib.request.urlopen", fake_urlopen):
        out = client.generate("Translate: Hello world.")
    assert out == "Halo dunia."
    # Pastikan payload OpenAI-compatible
    assert captured["url"].endswith("/chat/completions")
    assert captured["body"]["model"] == "claude-sonnet-4"
    assert captured["body"]["messages"][0]["role"] == "user"
    assert captured["body"]["messages"][0]["content"] == "Translate: Hello world."
    # Auth header (case-insensitive lookup)
    auth = next(v for k, v in captured["headers"].items() if k.lower() == "authorization")
    assert auth == "Bearer fake"


def test_runeria_generate_uses_glossary_model_when_specified():
    cfg = {"engine": "runeria", "runeria": {
        "api_key": "fake", "requests_per_minute": 0,
        "model": "deepseek-3.2", "glossary_model": "claude-sonnet-4",
    }}
    client = translate.create_llm_client(cfg, _silent_logger())

    seen_models = []

    class _FakeResp:
        def __init__(self, payload):
            self._buf = json.dumps(payload).encode("utf-8")
        def read(self): return self._buf
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        seen_models.append(body["model"])
        return _FakeResp({"choices": [{"message": {"content": "ok"}}]})

    with patch("urllib.request.urlopen", fake_urlopen):
        client.generate("hello")
        client.generate("hello", model_name=client.glossary_model_name)

    assert seen_models == ["deepseek-3.2", "claude-sonnet-4"]


def test_runeria_empty_response_triggers_retry_and_fail():
    """Response kosong = error, harus retry sampai max_retries lalu fail."""
    cfg = {"engine": "runeria", "runeria": {
        "api_key": "fake", "requests_per_minute": 0,
        "max_retries": 2, "retry_base_delay": 0,
    }}
    client = translate.create_llm_client(cfg, _silent_logger())

    class _FakeResp:
        def read(self): return b'{"choices":[{"message":{"content":""},"finish_reason":"stop"}]}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        return _FakeResp()

    with patch("urllib.request.urlopen", fake_urlopen):
        try:
            client.generate("hello")
        except RuntimeError as e:
            assert "gagal" in str(e).lower()
            return
    raise AssertionError("Expected RuntimeError untuk response kosong")


def test_runeria_429_is_retried():
    """HTTP 429 harus retry, dan kalau berhasil di percobaan kedua, return ok."""
    import urllib.error

    cfg = {"engine": "runeria", "runeria": {
        "api_key": "fake", "requests_per_minute": 0,
        "max_retries": 3, "retry_base_delay": 0,
    }}
    client = translate.create_llm_client(cfg, _silent_logger())

    calls = {"n": 0}

    class _FakeResp:
        def __init__(self, payload):
            self._buf = json.dumps(payload).encode("utf-8")
        def read(self): return self._buf
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests",
                {}, io.BytesIO(b'{"error":"rate limit"}'),
            )
        return _FakeResp({"choices": [{"message": {"content": "ok lagi"}}]})

    with patch("urllib.request.urlopen", fake_urlopen):
        out = client.generate("hello")
    assert out == "ok lagi"
    assert calls["n"] == 2


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
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
