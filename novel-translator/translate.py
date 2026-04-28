#!/usr/bin/env python3
"""
Novel Translator — terjemahkan chapter novel (.txt) dari Bahasa Inggris/Jepang
ke Bahasa Indonesia menggunakan Google Gemini (free tier).

Pemakaian dasar:
    # 1. Letakkan chapter .txt di:  novels/<nama_novel>/source/
    # 2. (Opsional) Build glossary: python translate.py --novel <nama> --build-glossary
    # 3. Terjemahkan semua chapter:  python translate.py --novel <nama>

Pemakaian lanjutan:
    python translate.py --novel my_novel --lang jp        # paksa bahasa sumber
    python translate.py --novel my_novel --only 1-10      # range chapter
    python translate.py --novel my_novel --rebuild        # timpa hasil terjemahan
    python translate.py --list                            # daftar novel yang terdeteksi
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from filters import FilterEngine

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml belum terpasang. Jalankan: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

# Lazy import google.generativeai supaya error message-nya jelas
def _import_genai():
    try:
        import google.generativeai as genai  # type: ignore
        from google.generativeai.types import HarmCategory, HarmBlockThreshold  # type: ignore
        return genai, HarmCategory, HarmBlockThreshold
    except ImportError:
        print(
            "ERROR: google-generativeai belum terpasang.\n"
            "Jalankan: pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)


ROOT = Path(__file__).resolve().parent
PROMPTS_DIR = ROOT / "prompts"
NOVELS_DIR = ROOT / "novels"
LOGS_DIR = ROOT / "logs"
CONFIG_PATH = ROOT / "config.yaml"


# ============================================================
# Konfigurasi & Logging
# ============================================================

def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"config.yaml tidak ditemukan di {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(novel_name: str, cfg: dict[str, Any]) -> logging.Logger:
    LOGS_DIR.mkdir(exist_ok=True)
    level = getattr(logging, cfg.get("logging", {}).get("level", "INFO").upper())
    logger = logging.getLogger("novel_translator")
    logger.setLevel(level)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if cfg.get("logging", {}).get("log_to_file", True):
        fh = logging.FileHandler(LOGS_DIR / f"{novel_name}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


# ============================================================
# Deteksi bahasa (heuristik sederhana, tanpa dependensi tambahan)
# ============================================================

CJK_IDEOGRAPHS = re.compile(r"[\u4E00-\u9FFF\u3400-\u4DBF]")
HIRAGANA_KATAKANA = re.compile(r"[\u3040-\u309F\u30A0-\u30FF\uFF66-\uFF9F]")
HANGUL = re.compile(r"[\uAC00-\uD7AF\u1100-\u11FF\u3130-\u318F]")

SUPPORTED_LANGS = ("en", "jp", "kr", "cn")


def detect_language(text: str) -> str:
    """Return 'jp', 'kr', 'cn', atau 'en' berdasarkan rasio karakter."""
    sample = text[:5000]
    total = max(len(sample), 1)
    hangul = len(HANGUL.findall(sample))
    kana = len(HIRAGANA_KATAKANA.findall(sample))
    cjk = len(CJK_IDEOGRAPHS.findall(sample))

    # Hangul (Korean) — paling distinctive, cek dulu
    if hangul / total > 0.01:
        return "kr"
    # Kana (Jepang)
    if kana / total > 0.01:
        return "jp"
    # Hanzi tanpa kana/hangul → Mandarin
    if cjk / total > 0.05:
        return "cn"
    return "en"


# ============================================================
# Glossary
# ============================================================

@dataclass
class Glossary:
    characters: dict[str, str] = field(default_factory=dict)
    places: dict[str, str] = field(default_factory=dict)
    terms: dict[str, str] = field(default_factory=dict)
    style_notes: str = ""

    @classmethod
    def load(cls, path: Path) -> "Glossary":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"glossary.json rusak ({path}): {e}")
        return cls(
            characters=data.get("characters", {}) or {},
            places=data.get("places", {}) or {},
            terms=data.get("terms", {}) or {},
            style_notes=data.get("style_notes", "") or "",
        )

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "characters": self.characters,
            "places": self.places,
            "terms": self.terms,
            "style_notes": self.style_notes,
        }

    def is_empty(self) -> bool:
        return not (self.characters or self.places or self.terms or self.style_notes)

    def merge(self, other: "Glossary") -> dict[str, int]:
        """Merge other ke self. Existing entries menang (jangan di-overwrite).

        Return dict berisi jumlah entry BARU yang ditambahkan per kategori.
        """
        added = {"characters": 0, "places": 0, "terms": 0}
        for k, v in other.characters.items():
            if k not in self.characters:
                self.characters[k] = v
                added["characters"] += 1
        for k, v in other.places.items():
            if k not in self.places:
                self.places[k] = v
                added["places"] += 1
        for k, v in other.terms.items():
            if k not in self.terms:
                self.terms[k] = v
                added["terms"] += 1
        if not self.style_notes and other.style_notes:
            self.style_notes = other.style_notes
        return added

    def format_for_prompt(self) -> str:
        if self.is_empty():
            return ""
        lines = ["GLOSSARY (WAJIB DIPATUHI agar terjemahan konsisten):"]
        if self.characters:
            lines.append("\n[Karakter]")
            for src, tgt in self.characters.items():
                lines.append(f"  {src} → {tgt}")
        if self.places:
            lines.append("\n[Tempat]")
            for src, tgt in self.places.items():
                lines.append(f"  {src} → {tgt}")
        if self.terms:
            lines.append("\n[Istilah]")
            for src, tgt in self.terms.items():
                lines.append(f"  {src} → {tgt}")
        if self.style_notes:
            lines.append(f"\n[Catatan Gaya]\n  {self.style_notes}")
        return "\n".join(lines) + "\n"


# ============================================================
# Gemini Client
# ============================================================

class LLMClient:
    """Common interface untuk LLM provider (Gemini, Runeria, dll).

    Subclass harus expose:
      - translate_model_name : str
      - glossary_model_name  : str
      - generate(prompt, *, model_name=None) -> str
    """

    translate_model_name: str = ""
    glossary_model_name: str = ""

    def generate(self, prompt: str, *, model_name: str | None = None) -> str:  # noqa: D401
        raise NotImplementedError


class GeminiClient(LLMClient):
    def __init__(self, cfg: dict[str, Any], logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger
        gcfg = cfg["gemini"]

        api_key = os.environ.get("GEMINI_API_KEY") or gcfg.get("api_key", "").strip()
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY belum diset. Set environment variable GEMINI_API_KEY "
                "atau isi gemini.api_key di config.yaml.\n"
                "Dapatkan API key gratis di: https://aistudio.google.com/apikey"
            )

        genai, HarmCategory, HarmBlockThreshold = _import_genai()
        genai.configure(api_key=api_key)
        self._genai = genai

        # Safety settings: BLOCK_NONE untuk semua kategori (untuk konten dewasa/kekerasan)
        self._safety = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }

        self._gen_config = {
            "temperature": float(gcfg.get("temperature", 0.3)),
            "max_output_tokens": int(gcfg.get("max_output_tokens", 16384)),
        }

        self.rpm = int(gcfg.get("requests_per_minute", 8))
        self.max_retries = int(gcfg.get("max_retries", 5))
        self.retry_base = float(gcfg.get("retry_base_delay", 5))
        self._last_call = 0.0

        self.translate_model_name = gcfg.get("model", "gemini-2.5-flash")
        self.glossary_model_name = gcfg.get("glossary_model", self.translate_model_name)

    def _rate_limit(self) -> None:
        if self.rpm <= 0:
            return
        min_interval = 60.0 / self.rpm
        now = time.time()
        wait = min_interval - (now - self._last_call)
        if wait > 0:
            self.logger.debug("Rate limit: tunggu %.1fs", wait)
            time.sleep(wait)
        self._last_call = time.time()

    def generate(self, prompt: str, *, model_name: str | None = None) -> str:
        model_name = model_name or self.translate_model_name
        model = self._genai.GenerativeModel(
            model_name,
            generation_config=self._gen_config,
            safety_settings=self._safety,
        )

        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._rate_limit()
            try:
                resp = model.generate_content(prompt)
                # Cek block reason
                if getattr(resp, "prompt_feedback", None):
                    block = getattr(resp.prompt_feedback, "block_reason", None)
                    if block:
                        raise RuntimeError(f"Prompt diblokir Gemini: {block}")
                # Ambil text
                text = self._extract_text(resp)
                if not text or not text.strip():
                    finish = self._finish_reason(resp)
                    raise RuntimeError(f"Response kosong (finish_reason={finish})")
                return text
            except Exception as e:  # noqa: BLE001
                last_err = e
                msg = str(e)
                # Quota / 429: tunggu lebih lama
                is_quota = "429" in msg or "quota" in msg.lower() or "rate" in msg.lower()
                delay = self.retry_base * (2 ** (attempt - 1))
                if is_quota:
                    delay = max(delay, 30)
                self.logger.warning(
                    "Gemini gagal (attempt %d/%d): %s — retry dalam %.1fs",
                    attempt, self.max_retries, msg, delay,
                )
                if attempt < self.max_retries:
                    time.sleep(delay)
        raise RuntimeError(f"Gemini gagal setelah {self.max_retries} percobaan: {last_err}")

    @staticmethod
    def _extract_text(resp: Any) -> str:
        # response.text bisa raise kalau tidak ada candidate; coba aman
        try:
            t = resp.text
            if t:
                return t
        except Exception:
            pass
        parts: list[str] = []
        for cand in getattr(resp, "candidates", []) or []:
            content = getattr(cand, "content", None)
            if not content:
                continue
            for part in getattr(content, "parts", []) or []:
                ptext = getattr(part, "text", None)
                if ptext:
                    parts.append(ptext)
        return "".join(parts)

    @staticmethod
    def _finish_reason(resp: Any) -> str:
        try:
            return str(resp.candidates[0].finish_reason)
        except Exception:
            return "unknown"


class OpenAICompatibleClient(LLMClient):
    """Client umum untuk provider OpenAI-compatible (POST <base>/chat/completions).

    Subclass cukup override class attribute berikut:
      provider_label    : str  -- nama untuk log (mis. "Runeria", "Cline")
      env_key_name      : str  -- nama env var (mis. "RUNERIA_API_KEY")
      config_section    : str  -- nama section di config.yaml (mis. "runeria")
      default_base_url  : str
      default_model     : str

    Format request OpenAI:
      POST <base_url>/chat/completions
      Authorization: Bearer <key>
      Body: {"model": ..., "messages": [{"role":"user","content":...}], ...}

    Format response:
      {"choices":[{"message":{"content":"..."},"finish_reason":...}]}
    """

    provider_label: str = "OpenAI-compatible"
    env_key_name: str = ""
    config_section: str = ""
    default_base_url: str = ""
    default_model: str = "gpt-4o-mini"

    def __init__(self, cfg: dict[str, Any], logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger
        rcfg = cfg.get(self.config_section, {}) or {}

        api_key = os.environ.get(self.env_key_name) or str(rcfg.get("api_key", "")).strip()
        if not api_key:
            raise RuntimeError(
                f"{self.env_key_name} belum diset. Set environment variable "
                f"{self.env_key_name} atau isi {self.config_section}.api_key "
                f"di config.yaml."
            )
        self.api_key = api_key
        self.base_url = str(rcfg.get("base_url", self.default_base_url)).rstrip("/")

        self.translate_model_name = rcfg.get("model", self.default_model)
        self.glossary_model_name = rcfg.get("glossary_model", self.translate_model_name)

        self.temperature = float(rcfg.get("temperature", 0.3))
        self.max_tokens = int(rcfg.get("max_output_tokens", 16384))

        self.rpm = int(rcfg.get("requests_per_minute", 30))
        self.max_retries = int(rcfg.get("max_retries", 5))
        self.retry_base = float(rcfg.get("retry_base_delay", 5))
        self.timeout = float(rcfg.get("timeout_seconds", 120))
        self._last_call = 0.0

    def _rate_limit(self) -> None:
        if self.rpm <= 0:
            return
        min_interval = 60.0 / self.rpm
        now = time.time()
        wait = min_interval - (now - self._last_call)
        if wait > 0:
            self.logger.debug("Rate limit: tunggu %.1fs", wait)
            time.sleep(wait)
        self._last_call = time.time()

    def _post(self, body: bytes) -> dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw)

    def generate(self, prompt: str, *, model_name: str | None = None) -> str:
        model_name = model_name or self.translate_model_name
        body = json.dumps({
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }).encode("utf-8")

        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._rate_limit()
            try:
                data = self._post(body)
                # OpenAI-compatible format
                choice = (data.get("choices") or [{}])[0]
                msg = (choice.get("message") or {})
                text = msg.get("content") or ""
                if not text or not text.strip():
                    finish = choice.get("finish_reason", "unknown")
                    raise RuntimeError(f"Response kosong (finish_reason={finish})")
                return text
            except urllib.error.HTTPError as e:
                last_err = e
                try:
                    body_text = e.read().decode("utf-8", errors="replace")
                except Exception:
                    body_text = ""
                snippet = body_text[:300]
                # Fail-fast untuk error 4xx yang permanen (tidak akan pernah
                # berhasil meskipun di-retry): 400 bad request, 401 auth, 403
                # forbidden (mis. model butuh plan Pro), 404 model tidak ada.
                # Pengecualian: 408 (request timeout) & 429 (rate limit) tetap
                # di-retry. Selain itu (5xx, network), retry dengan backoff.
                is_retryable_4xx = e.code in (408, 429)
                is_permanent = 400 <= e.code < 500 and not is_retryable_4xx
                if is_permanent:
                    self.logger.error(
                        "%s GAGAL (HTTP %d, permanen — tidak di-retry): %s",
                        self.provider_label, e.code, snippet,
                    )
                    raise RuntimeError(
                        f"{self.provider_label} HTTP {e.code} (permanen): {snippet}"
                    ) from e
                is_quota = e.code == 429 or "quota" in body_text.lower() or "rate" in body_text.lower()
                delay = self.retry_base * (2 ** (attempt - 1))
                if is_quota:
                    delay = max(delay, 30)
                self.logger.warning(
                    "%s gagal (attempt %d/%d): HTTP %d %s — retry dalam %.1fs",
                    self.provider_label, attempt, self.max_retries, e.code, snippet, delay,
                )
                if attempt < self.max_retries:
                    time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as e:
                last_err = e
                delay = self.retry_base * (2 ** (attempt - 1))
                self.logger.warning(
                    "%s gagal (attempt %d/%d): %s — retry dalam %.1fs",
                    self.provider_label, attempt, self.max_retries, e, delay,
                )
                if attempt < self.max_retries:
                    time.sleep(delay)
        raise RuntimeError(
            f"{self.provider_label} gagal setelah {self.max_retries} percobaan: {last_err}"
        )


class RuneriaClient(OpenAICompatibleClient):
    """Runeria (https://runeria.fun) — OpenAI-compatible.

    Plan basic mendukung: claude-sonnet-4, claude-haiku-4.5.
    Plan Pro/Enterprise: deepseek-3.2 dst (basic plan akan dapat HTTP 403).
    """

    provider_label = "Runeria"
    env_key_name = "RUNERIA_API_KEY"
    config_section = "runeria"
    default_base_url = "https://runeria.fun/v1"
    default_model = "claude-sonnet-4"


class ClineClient(OpenAICompatibleClient):
    """Cline (https://app.cline.bot) — OpenAI-compatible.

    Endpoint: https://api.cline.bot/api/v1/chat/completions
    Models tersedia: moonshotai/kimi-k2.6, minimax/minimax-m2.5,
                     kwaipilot/kat-coder-pro, z-ai/glm-5.
    """

    provider_label = "Cline"
    env_key_name = "CLINE_API_KEY"
    config_section = "cline"
    default_base_url = "https://api.cline.bot/api/v1"
    default_model = "moonshotai/kimi-k2.6"


SUPPORTED_ENGINES = ("gemini", "runeria", "cline")

# Daftar model yang direkomendasikan per-engine (dipakai menu interaktif &
# untuk validasi ringan). Ini bukan whitelist — user bebas isi model lain
# di config.yaml.
ENGINE_MODELS: dict[str, list[str]] = {
    "gemini": [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.5-pro",
    ],
    "runeria": [
        "claude-sonnet-4",      # basic plan (rekomendasi)
        "claude-haiku-4.5",     # basic plan (lebih cepat & murah, sedikit di bawah sonnet)
        "deepseek-3.2",         # PRO plan only
        "minimax-m2.5",         # cek plan
        "minimax-m2.1",         # cek plan
        "glm-5",                # cek plan
        "qwen3-coder-next",     # SKIP (coder model)
    ],
    "cline": [
        "moonshotai/kimi-k2.6",     # rekomendasi default
        "minimax/minimax-m2.5",
        "z-ai/glm-5",
        "kwaipilot/kat-coder-pro",  # SKIP (coder model)
    ],
}


def create_llm_client(
    cfg: dict[str, Any],
    logger: logging.Logger,
    engine_override: str | None = None,
) -> LLMClient:
    """Factory pilih LLM provider berdasarkan config / CLI override."""
    engine = (engine_override or str(cfg.get("engine") or "gemini")).lower().strip()
    if engine == "gemini":
        return GeminiClient(cfg, logger)
    if engine == "runeria":
        return RuneriaClient(cfg, logger)
    if engine == "cline":
        return ClineClient(cfg, logger)
    raise ValueError(
        f"Engine '{engine}' tidak dikenal. Pilih: {', '.join(SUPPORTED_ENGINES)}."
    )


# ============================================================
# Chunking
# ============================================================

def chunk_text(text: str, max_chars: int, overlap: int = 0) -> list[str]:
    """Pecah text jadi beberapa chunk pada batas paragraf jika memungkinkan."""
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    cur = ""
    for p in paragraphs:
        if len(cur) + len(p) + 2 <= max_chars:
            cur += (("\n\n" if cur else "") + p)
        else:
            if cur:
                chunks.append(cur)
            # Kalau satu paragraf saja > max_chars, paksa split
            if len(p) > max_chars:
                for i in range(0, len(p), max_chars):
                    chunks.append(p[i:i + max_chars])
                cur = ""
            else:
                cur = p
    if cur:
        chunks.append(cur)

    if overlap > 0 and len(chunks) > 1:
        # Tambah overlap dari akhir chunk sebelumnya ke awal chunk berikutnya
        out = [chunks[0]]
        for i in range(1, len(chunks)):
            prev_tail = chunks[i - 1][-overlap:]
            out.append(prev_tail + "\n\n" + chunks[i])
        return out
    return chunks


# ============================================================
# Post-processing: normalisasi kata ganti formal -> kasual
# ============================================================

# Penanda formal di paragraf yang membuat post-processing skip paragraf itu.
# Kalau salah satu pola ini muncul di paragraf, "Anda/Saya" dibiarkan apa adanya.
_FORMAL_MARKERS = re.compile(
    r"\b(Yang Mulia|Paduka|Baginda|Sri Baginda|Tuan(?: Putri| Muda| Besar)?|Nyonya|Nona|"
    r"Jenderal|Komandan|Kapten|Yang Terhormat|Hamba|Dengan hormat|Hormat saya|"
    r"Shifu|Shixiong|Shijie|Shidi|Shimei|Senpai|Sensei|Tuanku)\b",
    re.IGNORECASE,
)

# Pasangan substitusi kasual (urut dari yang paling spesifik ke umum supaya tidak bentrok).
# Pakai negative lookbehind/ahead untuk hindari merusak kata lain (misal "andaikan", "sayur").
_PRONOUN_SUBS_DIALOG: tuple[tuple[re.Pattern[str], str], ...] = (
    # "Anda" (huruf besar - bentuk standar)
    (re.compile(r"(?<![A-Za-zÀ-ÿ])Anda(?![A-Za-zÀ-ÿ])"), "kamu"),
    # "anda" (jarang, biasanya typo)
    (re.compile(r"(?<![A-Za-zÀ-ÿ])anda(?![A-Za-zÀ-ÿ])"), "kamu"),
    # "Saya" di awal kalimat / setelah tanda baca
    (re.compile(r"(?<![A-Za-zÀ-ÿ])Saya(?![A-Za-zÀ-ÿ])"), "Aku"),
    # "saya" lowercase
    (re.compile(r"(?<![A-Za-zÀ-ÿ])saya(?![A-Za-zÀ-ÿ])"), "aku"),
)


def _has_formal_marker(paragraph: str) -> bool:
    return bool(_FORMAL_MARKERS.search(paragraph))


def normalize_pronouns(
    text: str,
    *,
    strength: str = "safe",
    logger: logging.Logger | None = None,
) -> str:
    """
    Normalisasi 'Anda/Saya' -> 'kamu/aku' setelah Gemini menerjemahkan.

    Mode 'strength':
      - safe   : skip paragraf yang mengandung penanda formal (Yang Mulia, Tuan, dll)
      - aggressive : ganti semua tanpa pengecualian
      - off / lainnya : tidak melakukan apa-apa

    Default 'safe'. Cara kerja paragraf-by-paragraf supaya konteks formal terjaga.
    """
    if strength not in ("safe", "aggressive"):
        return text

    paragraphs = text.split("\n\n")
    changed = 0
    out: list[str] = []
    for para in paragraphs:
        if strength == "safe" and _has_formal_marker(para):
            out.append(para)
            continue
        new_para = para
        for pat, repl in _PRONOUN_SUBS_DIALOG:
            new_para = pat.sub(repl, new_para)
        if new_para != para:
            changed += 1
        out.append(new_para)

    if logger and changed:
        logger.info(
            "post-process: normalisasi kata ganti di %d paragraf (mode=%s)",
            changed, strength,
        )
    return "\n\n".join(out)


# ============================================================
# Penerjemah
# ============================================================

@dataclass
class NovelPaths:
    name: str
    root: Path
    source: Path
    translated: Path
    glossary_path: Path
    progress_path: Path
    filters_path: Path

    @classmethod
    def from_name(cls, name: str) -> "NovelPaths":
        root = NOVELS_DIR / name
        return cls(
            name=name,
            root=root,
            source=root / "source",
            translated=root / "translated",
            glossary_path=root / "glossary.json",
            progress_path=root / ".progress",
            filters_path=root / "filters.txt",
        )

    def ensure(self) -> None:
        self.source.mkdir(parents=True, exist_ok=True)
        self.translated.mkdir(parents=True, exist_ok=True)


def list_chapters(np: NovelPaths) -> list[Path]:
    files = sorted(np.source.glob("*.txt"), key=natural_sort_key)
    return files


def natural_sort_key(p: Path) -> list[Any]:
    """Sort 'Chapter_2' sebelum 'Chapter_10'."""
    parts = re.split(r"(\d+)", p.stem)
    return [int(s) if s.isdigit() else s.lower() for s in parts]


def load_progress(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_progress(path: Path, progress: dict[str, str]) -> None:
    path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


LANG_LABELS = {
    "en": "Bahasa Inggris",
    "jp": "Bahasa Jepang",
    "kr": "Bahasa Korea",
    "cn": "Bahasa Mandarin",
}

LANG_LABELS_PROMPT = {
    "en": "English",
    "jp": "Japanese",
    "kr": "Korean",
    "cn": "Chinese",
}


def load_prompt_template(lang: str) -> str:
    name = f"translate_{lang}.txt"
    path = PROMPTS_DIR / name
    if not path.exists():
        # Fallback ke EN kalau prompt khusus belum ada
        path = PROMPTS_DIR / "translate_en.txt"
    return path.read_text(encoding="utf-8")


def build_translation_prompt(template: str, text: str, glossary: Glossary) -> str:
    gblock = glossary.format_for_prompt()
    return template.replace("{glossary_block}", gblock).replace("{text}", text)


def translate_chapter(
    client: LLMClient,
    text: str,
    lang: str,
    glossary: Glossary,
    cfg: dict[str, Any],
    logger: logging.Logger,
) -> str:
    template = load_prompt_template(lang)
    max_chars = int(cfg["translation"].get("max_chars_per_chunk", 8000))
    overlap = int(cfg["translation"].get("chunk_overlap_chars", 0))

    chunks = chunk_text(text, max_chars=max_chars, overlap=overlap)
    if len(chunks) > 1:
        logger.info("Chapter dipecah jadi %d chunk", len(chunks))

    out_parts: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            logger.info("  → chunk %d/%d (%d chars)", i, len(chunks), len(chunk))
        prompt = build_translation_prompt(template, chunk, glossary)
        result = client.generate(prompt)
        out_parts.append(result.strip())

    return "\n\n".join(out_parts)


# ============================================================
# Glossary auto-extract
# ============================================================

def extract_glossary_from_chapters(
    client: LLMClient,
    chapters: list[Path],
    lang: str,
    n_samples: int,
    logger: logging.Logger,
) -> Glossary:
    samples = chapters[:n_samples]
    if not samples:
        return Glossary()

    logger.info("Membangun glossary dari %d chapter awal …", len(samples))
    template = (PROMPTS_DIR / "extract_glossary.txt").read_text(encoding="utf-8")

    # Gabungkan ringkas: sample maksimal ~10rb char total
    combined = ""
    per_chap_limit = max(2000, 10000 // max(len(samples), 1))
    for ch in samples:
        body = ch.read_text(encoding="utf-8", errors="replace")
        combined += f"\n\n=== {ch.name} ===\n{body[:per_chap_limit]}"

    src_lang_label = LANG_LABELS_PROMPT.get(lang, "English")
    prompt = template.replace("{source_language}", src_lang_label).replace("{text}", combined)

    raw = client.generate(prompt, model_name=client.glossary_model_name)
    return parse_glossary_json(raw, logger)


def parse_glossary_json(raw: str, logger: logging.Logger) -> Glossary:
    text = raw.strip()
    # Buang ```json ... ``` kalau ada
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    # Coba cari blok JSON pertama
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error("Gagal parse glossary JSON: %s\nRaw:\n%s", e, raw[:500])
        return Glossary()
    return Glossary(
        characters=data.get("characters", {}) or {},
        places=data.get("places", {}) or {},
        terms=data.get("terms", {}) or {},
        style_notes=data.get("style_notes", "") or "",
    )


# ============================================================
# CLI utama
# ============================================================

def parse_range(spec: str, total: int) -> set[int]:
    """Parse '1-10,15,20-25' ke set indeks 1-based."""
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return {i for i in out if 1 <= i <= total}


def cmd_list(_args: argparse.Namespace) -> int:
    if not NOVELS_DIR.exists():
        print("(belum ada novel — buat folder novels/<nama>/source/ lalu masukkan .txt)")
        return 0
    found = False
    for d in sorted(NOVELS_DIR.iterdir()):
        if not d.is_dir():
            continue
        src = d / "source"
        n = len(list(src.glob("*.txt"))) if src.exists() else 0
        translated = d / "translated"
        t = len(list(translated.glob("*.txt"))) if translated.exists() else 0
        gloss = "yes" if (d / "glossary.json").exists() else "no"
        print(f"  {d.name:30s}  source={n:4d}  translated={t:4d}  glossary={gloss}")
        found = True
    if not found:
        print("(belum ada novel)")
    return 0


def cmd_translate(args: argparse.Namespace) -> int:
    cfg = load_config()
    np = NovelPaths.from_name(args.novel)
    np.ensure()
    logger = setup_logging(args.novel, cfg)

    chapters = list_chapters(np)
    if not chapters:
        logger.error("Tidak ada file .txt di %s", np.source)
        logger.error("Letakkan chapter .txt di folder tersebut lalu jalankan lagi.")
        return 1
    logger.info("Ditemukan %d chapter di %s", len(chapters), np.source)

    # Filter range
    if args.only:
        wanted = parse_range(args.only, len(chapters))
        chapters = [c for i, c in enumerate(chapters, 1) if i in wanted]
        logger.info("Filter --only: %d chapter dipilih", len(chapters))

    # Filter engine: bersihkan boilerplate dari source SEBELUM dikirim ke Gemini.
    # Disetup di awal supaya --dry-run-filter bisa jalan tanpa API key &
    # tanpa memicu glossary auto-build.
    filt_cfg = cfg.get("filters", {}) or {}
    filter_engine = FilterEngine.from_config(filt_cfg, custom_patterns_path=np.filters_path)
    filter_pre = bool(filt_cfg.get("apply_pre_translation", True)) and bool(filter_engine.enabled)
    filter_post = bool(filt_cfg.get("apply_post_translation", True)) and bool(filter_engine.enabled)

    # --dry-run-filter: preview baris yang AKAN dihapus, tanpa init Gemini
    # client, tanpa build glossary, tanpa menerjemahkan apa pun. Murni operasi
    # lokal supaya tidak menghabiskan kuota API.
    if getattr(args, "dry_run_filter", False):
        any_match = False
        for src_path in chapters:
            text = src_path.read_text(encoding="utf-8", errors="replace")
            matches = filter_engine.dry_run(text)
            if not matches:
                continue
            any_match = True
            logger.info("[%s] %d baris akan dihapus:", src_path.name, len(matches))
            for line_no, line, cat in matches:
                logger.info("  L%-4d [%s] %s", line_no, cat, line.strip())
        if not any_match:
            logger.info("Tidak ada baris yang match filter di chapter yang dipilih.")
        logger.info("Mode --dry-run-filter: selesai (tidak ada chapter diterjemahkan).")
        return 0

    # Deteksi bahasa
    if args.lang:
        lang = args.lang
        logger.info("Bahasa di-paksa via --lang: %s", lang)
    else:
        sample = chapters[0].read_text(encoding="utf-8", errors="replace")
        lang = detect_language(sample)
        logger.info("Bahasa terdeteksi: %s", lang)

    # Init Gemini client
    client = create_llm_client(cfg, logger, getattr(args, "engine", None))
    logger.info(
        "Engine: %s | model translate=%s | model glossary=%s",
        client.__class__.__name__, client.translate_model_name, client.glossary_model_name,
    )

    # Glossary
    glossary = Glossary.load(np.glossary_path)
    gmode = cfg["glossary"].get("mode", "auto")

    if args.build_glossary or (gmode == "auto" and glossary.is_empty()):
        n = int(cfg["glossary"].get("sample_chapters", 3))
        new_g = extract_glossary_from_chapters(client, chapters, lang, n, logger)
        added = glossary.merge(new_g)
        logger.info(
            "Glossary entry baru: +%d karakter, +%d tempat, +%d istilah",
            added["characters"], added["places"], added["terms"],
        )
        glossary.save(np.glossary_path)
        logger.info(
            "Glossary disimpan ke %s (%d karakter, %d tempat, %d istilah)",
            np.glossary_path, len(glossary.characters), len(glossary.places), len(glossary.terms),
        )
        if args.build_glossary:
            logger.info("Mode --build-glossary: selesai. Review lalu jalankan tanpa flag ini.")
            return 0
    elif gmode == "manual" and glossary.is_empty():
        logger.warning(
            "Mode glossary 'manual' tapi %s kosong/tidak ada. Lanjut tanpa glossary.",
            np.glossary_path,
        )

    # Progress
    progress = load_progress(np.progress_path)
    add_header = bool(cfg["output"].get("add_header", True))
    pattern = cfg["output"].get("filename_pattern", "{stem}.txt")
    pp_cfg = cfg.get("post_process", {}) or {}

    total = len(chapters)
    done = skipped = failed = 0

    # Auto-update glossary tiap N chapter selesai diterjemahkan.
    # 0 atau negatif = disabled.
    auto_every = int(cfg["glossary"].get("auto_update_every", 0) or 0)
    auto_buffer: list[Path] = []  # source chapter terbaru yang belum di-scan

    for idx, src_path in enumerate(chapters, 1):
        out_name = pattern.format(stem=src_path.stem)
        out_path = np.translated / out_name

        prefix = f"[{idx}/{total}] {src_path.name}"

        if out_path.exists() and not args.rebuild:
            logger.info("%s — sudah ada, skip (pakai --rebuild untuk timpa)", prefix)
            skipped += 1
            continue

        text = src_path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            logger.warning("%s — kosong, skip", prefix)
            skipped += 1
            continue

        # Pre-translation filter: hapus boilerplate dari source.
        if filter_pre:
            cleaned, fstats = filter_engine.clean(text)
            if fstats.total_removed() or fstats.html_substitutions:
                logger.info(
                    "%s — filter source: -%d baris (%s) -%d HTML residue",
                    prefix, fstats.total_removed(),
                    ", ".join(f"{k}={v}" for k, v in fstats.removed_lines.items()),
                    fstats.html_substitutions,
                )
            text = cleaned
            if not text.strip():
                logger.warning("%s — kosong setelah filter, skip", prefix)
                skipped += 1
                continue

        logger.info("%s — menerjemahkan (%d chars) …", prefix, len(text))
        try:
            translated = translate_chapter(client, text, lang, glossary, cfg, logger)
        except Exception as e:  # noqa: BLE001
            logger.error("%s — GAGAL: %s", prefix, e)
            progress[src_path.name] = f"failed: {e}"
            save_progress(np.progress_path, progress)
            failed += 1
            continue

        # Optional post-processing: normalisasi kata ganti formal -> kasual.
        if pp_cfg.get("normalize_pronouns", False):
            translated = normalize_pronouns(
                translated,
                strength=str(pp_cfg.get("normalize_pronouns_strength", "safe")),
                logger=logger,
            )

        # Post-translation filter: bersihkan residu yang lolos dari Gemini
        # (HTML residue, baris boilerplate yang ikut tertranslate, whitespace).
        if filter_post:
            translated, post_stats = filter_engine.clean(translated)
            if post_stats.total_removed() or post_stats.html_substitutions:
                logger.info(
                    "%s — filter output: -%d baris -%d HTML residue",
                    prefix, post_stats.total_removed(), post_stats.html_substitutions,
                )

        body = translated
        if add_header:
            src_label = LANG_LABELS.get(lang, "Bahasa Inggris")
            header = (
                f"# {src_path.stem}\n"
                f"# (Diterjemahkan otomatis dari {src_label} ke Bahasa Indonesia)\n\n"
            )
            body = header + body

        out_path.write_text(body, encoding="utf-8")
        progress[src_path.name] = "done"
        save_progress(np.progress_path, progress)
        done += 1
        logger.info("%s — OK → %s", prefix, out_path.relative_to(ROOT))

        # Auto-update glossary: setiap auto_every chapter berhasil, scan
        # batch chapter source terbaru untuk nama/istilah baru, merge
        # non-destructively.
        if auto_every > 0:
            auto_buffer.append(src_path)
            if len(auto_buffer) >= auto_every:
                _auto_update_glossary(
                    client, glossary, auto_buffer, lang, np.glossary_path, logger,
                )
                auto_buffer.clear()

    # Sisa buffer di akhir run: kalau ada >=1, scan supaya glossary up-to-date
    # untuk run berikutnya.
    if auto_every > 0 and auto_buffer:
        _auto_update_glossary(
            client, glossary, auto_buffer, lang, np.glossary_path, logger,
        )

    logger.info("Selesai. done=%d skipped=%d failed=%d", done, skipped, failed)
    return 0 if failed == 0 else 2


def _auto_update_glossary(
    client: GeminiClient,
    glossary: Glossary,
    chapters: list[Path],
    lang: str,
    glossary_path: Path,
    logger: logging.Logger,
) -> None:
    """Scan chapter terbaru untuk entry glossary baru, merge & save.

    Existing entry tidak di-overwrite (lihat Glossary.merge). Hanya entry
    yang benar-benar BARU yang di-tambahkan ke glossary.json.
    """
    logger.info("Auto-update glossary: scan %d chapter terbaru …", len(chapters))
    try:
        new_g = extract_glossary_from_chapters(
            client, chapters, lang, len(chapters), logger,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Auto-update glossary GAGAL (lanjut tanpa update): %s", e)
        return

    added = glossary.merge(new_g)
    total_new = added["characters"] + added["places"] + added["terms"]
    if total_new > 0:
        glossary.save(glossary_path)
        logger.info(
            "Auto-update glossary: +%d karakter, +%d tempat, +%d istilah baru → disimpan",
            added["characters"], added["places"], added["terms"],
        )
    else:
        logger.info("Auto-update glossary: tidak ada entry baru.")


# ============================================================
# Interactive menu mode
# ============================================================
#
# Tujuan: bikin tool dipakai tanpa harus hafal flag CLI. Jalanin
# `python translate.py` tanpa argumen apa pun -> muncul menu.
# Semua action delegate ke fungsi cmd_* yang sudah ada (sehingga CLI
# & menu tidak duplikat logika).

def _menu_print(text: str = "") -> None:
    print(text)


def _menu_input(prompt: str, default: str | None = None) -> str:
    """Wrapper input() dengan support default. Ctrl-C / Ctrl-D return string kosong."""
    try:
        full_prompt = f"{prompt} [{default}]: " if default else f"{prompt}: "
        ans = input(full_prompt).strip()
        return ans or (default or "")
    except (EOFError, KeyboardInterrupt):
        return ""


def _menu_list_novels() -> list[Path]:
    if not NOVELS_DIR.exists():
        return []
    return sorted([d for d in NOVELS_DIR.iterdir() if d.is_dir() and (d / "source").exists()])


def _menu_pick_novel() -> str | None:
    """Tampilkan list novel, return nama (string) yang dipilih atau None."""
    novels = _menu_list_novels()
    if not novels:
        _menu_print("  (belum ada novel — buat folder novels/<nama>/source/ dulu)")
        return None
    _menu_print()
    for i, d in enumerate(novels, 1):
        n = len(list((d / "source").glob("*.txt")))
        t_dir = d / "translated"
        t = len(list(t_dir.glob("*.txt"))) if t_dir.exists() else 0
        gloss = "yes" if (d / "glossary.json").exists() else "no "
        _menu_print(f"  {i:2d}. {d.name:30s}  source={n:4d}  translated={t:4d}  glossary={gloss}")
    _menu_print(f"   0. (batal)")
    raw = _menu_input("Pilih novel (nomor)")
    if not raw or raw == "0":
        return None
    try:
        idx = int(raw)
        if 1 <= idx <= len(novels):
            return novels[idx - 1].name
    except ValueError:
        pass
    _menu_print("  Pilihan tidak valid.")
    return None


def _menu_engine_label(cfg: dict[str, Any]) -> str:
    engine = str(cfg.get("engine") or "gemini").lower().strip()
    section_cfg = cfg.get(engine, {}) or {}
    if engine == "gemini":
        model = section_cfg.get("model") or "gemini-2.5-flash"
    else:
        model = section_cfg.get("model") or ENGINE_MODELS.get(engine, ["?"])[0]
    return f"engine={engine}  model={model}"


def _patch_config_top_level(path: Path, key: str, value: str) -> bool:
    """Replace top-level `key: ...` line in YAML, preserve comments. Return True if patched."""
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    pattern = re.compile(rf"^{re.escape(key)}\s*:\s*")
    for i, line in enumerate(lines):
        # Top-level = no leading whitespace, line not a comment.
        if line.startswith((" ", "\t")) or line.lstrip().startswith("#"):
            continue
        if pattern.match(line):
            lines[i] = f'{key}: "{value}"'
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True
    return False


def _patch_config_nested(path: Path, section: str, key: str, value: str) -> bool:
    """Replace `<key>: ...` line inside `<section>:` block. Preserve comments."""
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    section_re = re.compile(rf"^{re.escape(section)}\s*:\s*$")
    key_re = re.compile(rf"^(\s+){re.escape(key)}\s*:\s*")
    in_section = False
    for i, line in enumerate(lines):
        if section_re.match(line):
            in_section = True
            continue
        if in_section:
            # Akhir block: ketemu top-level key lain (no indent, bukan comment/blank).
            stripped = line.lstrip()
            if line and not line.startswith((" ", "\t")) and stripped and not stripped.startswith("#"):
                in_section = False
                continue
            m = key_re.match(line)
            if m:
                lines[i] = f'{m.group(1)}{key}: "{value}"'
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return True
    return False


def _menu_switch_engine() -> bool:
    """Sub-menu ganti engine & model. Return True kalau ada perubahan disimpan permanen."""
    _menu_print()
    _menu_print("--- Ganti engine & model ---")
    engines = list(SUPPORTED_ENGINES)
    for i, e in enumerate(engines, 1):
        _menu_print(f"  {i}. {e}")
    _menu_print(f"  0. (batal)")
    raw = _menu_input("Pilih engine")
    if not raw or raw == "0":
        return False
    try:
        engine = engines[int(raw) - 1]
    except (ValueError, IndexError):
        _menu_print("  Pilihan tidak valid.")
        return False

    models = ENGINE_MODELS.get(engine, [])
    _menu_print()
    _menu_print(f"Model untuk {engine}:")
    for i, m in enumerate(models, 1):
        _menu_print(f"  {i:2d}. {m}")
    _menu_print(f"   c. (custom — ketik nama model sendiri)")
    _menu_print(f"   0. (batal)")
    raw = _menu_input("Pilih model")
    if not raw or raw == "0":
        return False
    if raw.lower() == "c":
        model = _menu_input("Nama model")
        if not model:
            return False
    else:
        try:
            model = models[int(raw) - 1]
        except (ValueError, IndexError):
            _menu_print("  Pilihan tidak valid.")
            return False

    _menu_print()
    _menu_print(f"Pilihan: engine={engine}, model={model}")
    save = _menu_input("Simpan permanen ke config.yaml? (y/N)", default="N").lower()
    if save in ("y", "yes"):
        ok1 = _patch_config_top_level(CONFIG_PATH, "engine", engine)
        ok2 = _patch_config_nested(CONFIG_PATH, engine, "model", model)
        ok3 = _patch_config_nested(CONFIG_PATH, engine, "glossary_model", model)
        if ok1 and ok2:
            _menu_print(f"  Disimpan ke {CONFIG_PATH}.")
            if not ok3:
                _menu_print(f"  (catatan: glossary_model di section {engine} tidak ditemukan, dilewati)")
            return True
        _menu_print("  Gagal patch config.yaml — edit manual:")
        _menu_print(f"    engine: \"{engine}\"")
        _menu_print(f"    {engine}.model: \"{model}\"")
        return False
    _menu_print("  Tidak disimpan. (Untuk run sekali pakai, gunakan --engine ... --model lewat config sendiri.)")
    return False


def _menu_translate_flow(parser: "argparse.ArgumentParser") -> int:
    novel = _menu_pick_novel()
    if not novel:
        return 0
    rng = _menu_input("Range chapter (mis. '1-10' atau '1,3,5'; kosong=semua)")
    rebuild = _menu_input("Timpa hasil yang sudah ada? (y/N)", default="N").lower() in ("y", "yes")
    engine_override = ""
    if _menu_input("Override engine untuk run ini? (y/N)", default="N").lower() in ("y", "yes"):
        for i, e in enumerate(SUPPORTED_ENGINES, 1):
            _menu_print(f"  {i}. {e}")
        raw = _menu_input("Pilih engine")
        try:
            engine_override = SUPPORTED_ENGINES[int(raw) - 1]
        except (ValueError, IndexError):
            engine_override = ""
    argv = ["--novel", novel]
    if rng:
        argv += ["--only", rng]
    if rebuild:
        argv += ["--rebuild"]
    if engine_override:
        argv += ["--engine", engine_override]
    _menu_print(f"\n>> python translate.py {' '.join(argv)}")
    args = parser.parse_args(argv)
    return cmd_translate(args)


def _menu_glossary_flow(parser: "argparse.ArgumentParser") -> int:
    novel = _menu_pick_novel()
    if not novel:
        return 0
    while True:
        _menu_print()
        _menu_print(f"--- Glossary: {novel} ---")
        _menu_print("  1. List semua entry")
        _menu_print("  2. Add entry")
        _menu_print("  3. Edit entry")
        _menu_print("  4. Remove entry")
        _menu_print("  5. Set style notes")
        _menu_print("  0. Kembali")
        raw = _menu_input("Pilih")
        if not raw or raw == "0":
            return 0
        argv = ["--novel", novel]
        if raw == "1":
            argv += ["--glossary-list"]
        elif raw in ("2", "3"):
            t = _menu_input("Tipe (character | place | term)", default="character")
            src = _menu_input("Source (mis. 'Yukine')")
            tgt = _menu_input("Target (mis. 'Yukino')")
            if not (t and src and tgt):
                _menu_print("  Input tidak lengkap, batal.")
                continue
            argv += [
                "--glossary-add" if raw == "2" else "--glossary-edit",
                t, src, tgt,
            ]
        elif raw == "4":
            t = _menu_input("Tipe (character | place | term)", default="character")
            src = _menu_input("Source")
            if not (t and src):
                continue
            argv += ["--glossary-remove", t, src]
        elif raw == "5":
            note = _menu_input("Style notes (kosong = hapus)")
            argv += ["--glossary-set-style", note]
        else:
            _menu_print("  Pilihan tidak valid.")
            continue
        try:
            args = parser.parse_args(argv)
            cmd_glossary(args)
        except SystemExit:
            pass
        except (RuntimeError, ValueError, FileNotFoundError) as e:
            _menu_print(f"  ERROR: {e}")


def _menu_dry_run_flow(parser: "argparse.ArgumentParser") -> int:
    novel = _menu_pick_novel()
    if not novel:
        return 0
    args = parser.parse_args(["--novel", novel, "--dry-run-filter"])
    return cmd_translate(args)


def _menu_build_glossary_flow(parser: "argparse.ArgumentParser") -> int:
    novel = _menu_pick_novel()
    if not novel:
        return 0
    args = parser.parse_args(["--novel", novel, "--build-glossary"])
    return cmd_translate(args)


def _menu_help() -> None:
    _menu_print()
    _menu_print("=== Cheatsheet command CLI ===")
    _menu_print()
    _menu_print("# List semua novel")
    _menu_print("python translate.py --list")
    _menu_print()
    _menu_print("# Translate novel (auto-detect bahasa, default engine dari config)")
    _menu_print("python translate.py --novel <nama>")
    _menu_print("python translate.py --novel <nama> --only 1-10")
    _menu_print("python translate.py --novel <nama> --only 1,3,5-8")
    _menu_print("python translate.py --novel <nama> --rebuild   # timpa hasil lama")
    _menu_print()
    _menu_print("# Override engine sekali pakai")
    _menu_print("python translate.py --novel <nama> --engine runeria")
    _menu_print("python translate.py --novel <nama> --engine cline")
    _menu_print()
    _menu_print("# Glossary editor (offline, tanpa API key)")
    _menu_print("python translate.py --novel <nama> --glossary-list")
    _menu_print("python translate.py --novel <nama> --glossary-add character Yukine Yukino")
    _menu_print("python translate.py --novel <nama> --glossary-edit character Yukine Yuki")
    _menu_print("python translate.py --novel <nama> --glossary-remove character Yukine")
    _menu_print("python translate.py --novel <nama> --glossary-set-style \"Pakai honorifik JP.\"")
    _menu_print()
    _menu_print("# Build glossary saja, tidak translate")
    _menu_print("python translate.py --novel <nama> --build-glossary")
    _menu_print()
    _menu_print("# Preview baris yang akan dihapus filter (tanpa API call, tanpa translate)")
    _menu_print("python translate.py --novel <nama> --dry-run-filter")
    _menu_print()
    _menu_print("# Menu interaktif (yang sekarang sedang kamu jalankan)")
    _menu_print("python translate.py --menu     # atau jalan tanpa argumen apa pun")
    _menu_print()


def cmd_menu(parser: "argparse.ArgumentParser") -> int:
    """Loop menu interaktif. Tiap action delegate ke cmd_translate / cmd_glossary."""
    while True:
        try:
            cfg = load_config()
        except Exception as e:  # noqa: BLE001
            cfg = {}
            _menu_print(f"(peringatan: gagal load config.yaml: {e})")
        _menu_print()
        _menu_print("======================================")
        _menu_print(" Novel Translator — menu interaktif")
        _menu_print(f" {_menu_engine_label(cfg)}")
        _menu_print("======================================")
        _menu_print("  1. List semua novel & status")
        _menu_print("  2. Translate novel")
        _menu_print("  3. Edit glossary")
        _menu_print("  4. Ganti engine & model")
        _menu_print("  5. Build glossary saja (tanpa translate)")
        _menu_print("  6. Dry-run filter (preview baris yang dihapus)")
        _menu_print("  7. Cheatsheet command CLI")
        _menu_print("  0. Keluar")
        _menu_print()
        raw = _menu_input("Pilih")
        if not raw or raw == "0":
            return 0
        try:
            if raw == "1":
                cmd_list(parser.parse_args([]))
            elif raw == "2":
                _menu_translate_flow(parser)
            elif raw == "3":
                _menu_glossary_flow(parser)
            elif raw == "4":
                _menu_switch_engine()
            elif raw == "5":
                _menu_build_glossary_flow(parser)
            elif raw == "6":
                _menu_dry_run_flow(parser)
            elif raw == "7":
                _menu_help()
            else:
                _menu_print("  Pilihan tidak valid.")
        except (RuntimeError, ValueError, FileNotFoundError) as e:
            _menu_print(f"\nERROR: {e}\n")
        except KeyboardInterrupt:
            _menu_print("\n(dihentikan, kembali ke menu)")


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="translate.py",
        description="Terjemahkan chapter novel .txt (EN/JP) ke Bahasa Indonesia via Gemini.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd")

    p.add_argument("--novel", help="Nama folder novel di bawah novels/")
    p.add_argument(
        "--lang",
        choices=list(SUPPORTED_LANGS),
        help="Paksa bahasa sumber (default: auto-detect). Pilihan: en, jp, kr, cn.",
    )
    p.add_argument("--only", help="Range chapter, contoh: '1-10' atau '1,3,5-8'")
    p.add_argument("--engine", choices=SUPPORTED_ENGINES,
                   help="Override LLM engine (default dari config.yaml: gemini)")
    p.add_argument("--rebuild", action="store_true", help="Timpa file terjemahan yang sudah ada")
    p.add_argument("--build-glossary", action="store_true",
                   help="Bangun ulang glossary.json lalu berhenti (tidak menerjemahkan)")
    p.add_argument("--dry-run-filter", action="store_true",
                   help="Tampilkan baris source yang AKAN dihapus filter (tanpa menerjemahkan)")
    p.add_argument("--list", action="store_true", help="Daftar novel yang terdeteksi & statusnya")
    p.add_argument("--menu", action="store_true",
                   help="Jalankan menu interaktif (atau jalan tanpa argumen apa pun)")

    # ----- Glossary editor (offline, tidak butuh API key) -----
    g = p.add_argument_group(
        "Glossary editor",
        "Kelola glossary.json per-novel tanpa edit JSON manual. Tipe valid: "
        "character, place, term.",
    )
    g.add_argument(
        "--glossary-list", action="store_true",
        help="Tampilkan isi glossary novel terkait.",
    )
    g.add_argument(
        "--glossary-add", nargs=3, metavar=("TYPE", "SOURCE", "TARGET"),
        help="Tambah entry. Contoh: --glossary-add character Yukine Yukino",
    )
    g.add_argument(
        "--glossary-edit", nargs=3, metavar=("TYPE", "SOURCE", "NEW_TARGET"),
        help="Update entry yang sudah ada (overwrite target).",
    )
    g.add_argument(
        "--glossary-remove", nargs=2, metavar=("TYPE", "SOURCE"),
        help="Hapus entry. Contoh: --glossary-remove character Yukine",
    )
    g.add_argument(
        "--glossary-set-style", metavar="NOTES",
        help="Set style_notes (string kosong untuk hapus).",
    )

    sub.add_parser("list", help="alias untuk --list")
    return p


GLOSSARY_TYPES = {"character": "characters", "place": "places", "term": "terms"}


def cmd_glossary(args: argparse.Namespace) -> int:
    """Glossary editor offline. Tidak menyentuh API.

    Return 0 kalau ada operasi yang dijalankan, atau caller harus fallback
    ke cmd_translate kalau tidak ada flag glossary yang aktif.
    """
    np = NovelPaths.from_name(args.novel)
    np.ensure()
    glossary = Glossary.load(np.glossary_path)

    def _rel(p: Path) -> str:
        """Tampilkan path relatif ke ROOT kalau bisa, kalau tidak absolute."""
        try:
            return str(p.relative_to(ROOT))
        except ValueError:
            return str(p)

    def _resolve_type(t: str) -> str:
        t = t.lower().strip()
        if t in GLOSSARY_TYPES:
            return GLOSSARY_TYPES[t]
        # plural sebagai alias
        if t in GLOSSARY_TYPES.values():
            return t
        raise ValueError(
            f"Tipe '{t}' tidak valid. Pilih: character, place, term."
        )

    changed = False

    if args.glossary_list:
        if glossary.is_empty():
            print(f"({_rel(np.glossary_path)} kosong / belum ada)")
        else:
            print(f"# Glossary: {_rel(np.glossary_path)}\n")
            for kind, label in (
                ("characters", "Karakter"),
                ("places", "Tempat"),
                ("terms", "Istilah"),
            ):
                d = getattr(glossary, kind)
                if d:
                    print(f"[{label}] ({len(d)} entry)")
                    for src, tgt in d.items():
                        print(f"  {src!r:30s} -> {tgt!r}")
                    print()
            if glossary.style_notes:
                print(f"[Style notes]\n  {glossary.style_notes}")
        return 0

    if args.glossary_add:
        type_, src, tgt = args.glossary_add
        kind = _resolve_type(type_)
        d = getattr(glossary, kind)
        if src in d:
            print(
                f"ERROR: '{src}' sudah ada di {kind} (→ {d[src]!r}). "
                f"Pakai --glossary-edit untuk update.",
                file=sys.stderr,
            )
            return 1
        d[src] = tgt
        changed = True
        print(f"Added {kind}: {src!r} -> {tgt!r}")

    if args.glossary_edit:
        type_, src, new_tgt = args.glossary_edit
        kind = _resolve_type(type_)
        d = getattr(glossary, kind)
        if src not in d:
            print(
                f"ERROR: '{src}' tidak ada di {kind}. "
                f"Pakai --glossary-add untuk tambah baru.",
                file=sys.stderr,
            )
            return 1
        old = d[src]
        d[src] = new_tgt
        changed = True
        print(f"Edited {kind}: {src!r}: {old!r} -> {new_tgt!r}")

    if args.glossary_remove:
        type_, src = args.glossary_remove
        kind = _resolve_type(type_)
        d = getattr(glossary, kind)
        if src not in d:
            print(
                f"ERROR: '{src}' tidak ada di {kind}.",
                file=sys.stderr,
            )
            return 1
        old = d.pop(src)
        changed = True
        print(f"Removed {kind}: {src!r} (was → {old!r})")

    if args.glossary_set_style is not None:
        glossary.style_notes = args.glossary_set_style
        changed = True
        if args.glossary_set_style:
            print(f"Style notes diset: {args.glossary_set_style!r}")
        else:
            print("Style notes dihapus.")

    if changed:
        glossary.save(np.glossary_path)
        print(f"Saved → {_rel(np.glossary_path)}")
    return 0


def _has_glossary_flags(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "glossary_list", False)
        or getattr(args, "glossary_add", None)
        or getattr(args, "glossary_edit", None)
        or getattr(args, "glossary_remove", None)
        or getattr(args, "glossary_set_style", None) is not None
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_argparser()
    # Tanpa argumen apa pun -> menu interaktif (paling user-friendly).
    real_argv = sys.argv[1:] if argv is None else argv
    if not real_argv:
        return cmd_menu(parser)
    args = parser.parse_args(argv)
    if getattr(args, "menu", False):
        return cmd_menu(parser)

    if args.list or args.cmd == "list":
        return cmd_list(args)
    if not args.novel:
        print("ERROR: --novel <nama> wajib diisi (atau pakai --list / --menu).", file=sys.stderr)
        return 1
    try:
        # Glossary editor (offline, tidak butuh API key) di-handle duluan.
        if _has_glossary_flags(args):
            return cmd_glossary(args)
        return cmd_translate(args)
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nDihentikan oleh user.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
