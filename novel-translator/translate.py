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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

    def merge(self, other: "Glossary") -> None:
        # Existing entries menang (jangan di-overwrite)
        for k, v in other.characters.items():
            self.characters.setdefault(k, v)
        for k, v in other.places.items():
            self.places.setdefault(k, v)
        for k, v in other.terms.items():
            self.terms.setdefault(k, v)
        if not self.style_notes and other.style_notes:
            self.style_notes = other.style_notes

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

class GeminiClient:
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
    client: GeminiClient,
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
    client: GeminiClient,
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

    # Deteksi bahasa
    if args.lang:
        lang = args.lang
        logger.info("Bahasa di-paksa via --lang: %s", lang)
    else:
        sample = chapters[0].read_text(encoding="utf-8", errors="replace")
        lang = detect_language(sample)
        logger.info("Bahasa terdeteksi: %s", lang)

    # Init Gemini client
    client = GeminiClient(cfg, logger)

    # Glossary
    glossary = Glossary.load(np.glossary_path)
    gmode = cfg["glossary"].get("mode", "auto")

    if args.build_glossary or (gmode == "auto" and glossary.is_empty()):
        n = int(cfg["glossary"].get("sample_chapters", 3))
        new_g = extract_glossary_from_chapters(client, chapters, lang, n, logger)
        glossary.merge(new_g)
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

    logger.info("Selesai. done=%d skipped=%d failed=%d", done, skipped, failed)
    return 0 if failed == 0 else 2


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
    p.add_argument("--rebuild", action="store_true", help="Timpa file terjemahan yang sudah ada")
    p.add_argument("--build-glossary", action="store_true",
                   help="Bangun ulang glossary.json lalu berhenti (tidak menerjemahkan)")
    p.add_argument("--list", action="store_true", help="Daftar novel yang terdeteksi & statusnya")

    sub.add_parser("list", help="alias untuk --list")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)

    if args.list or args.cmd == "list":
        return cmd_list(args)
    if not args.novel:
        print("ERROR: --novel <nama> wajib diisi (atau pakai --list).", file=sys.stderr)
        return 1
    try:
        return cmd_translate(args)
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nDihentikan oleh user.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
