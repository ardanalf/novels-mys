"""
filters.py — Pembersihan boilerplate chapter sebelum & sesudah terjemahan.

Tujuan: chapter web novel yang di-scrape sering punya banyak boilerplate
(kredit translator, link Patreon, navigasi prev/next, dll) yang:
  1. Boros token kalau dikirim ke Gemini.
  2. Bisa ikut diterjemahkan dan ngotorin output.
  3. Tidak relevan untuk pembaca akhir.

Modul ini bekerja per-baris untuk REMOVAL (kategori A-G, I) dan substring
untuk HTML residue (kategori H), plus whitespace normalization (J) di akhir.

Kategori (toggleable lewat config.yaml -> filters.* atau argumen ke clean()):
  A. credits           - "Translated by", "TL:", "Editor:", source attribution
  B. donate            - Patreon, Ko-fi, PayPal, Buy Me a Coffee
  C. social            - Discord, Telegram, Twitter/X, Facebook, Instagram
  D. navigation        - "Prev | Next | Index", garis pemisah polos
  E. ads               - "Like and comment", "Subscribe", "Rate this novel"
  F. schedule          - "Sponsored chapter", "Bonus chapter", release schedule
  G. tl_notes          - Author/Translator notes (DEFAULT KEEP, opsional remove)
  H. html_residue      - &nbsp;, <p>, <br>, dll (substring replacement)
  I. footer            - "All rights reserved", "© 2024", "Do not repost"
  J. whitespace        - normalize multiple blank lines, trailing spaces
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

Matcher = Callable[[str], object]

# ----------------------------------------------------------------------
# Pola per kategori. Semua case-insensitive, di-anchor ke seluruh BARIS
# (kecuali kategori H yang substring).
# ----------------------------------------------------------------------

# Helper: matcher seluruh baris (boleh ada whitespace + tanda baca di tepi).
# Mengembalikan callable supaya kategori juga bisa berisi heuristic function
# selain regex.
def _line(pattern: str) -> Matcher:
    return re.compile(rf"^\s*{pattern}\s*$", re.IGNORECASE).match


# Token-level vocabulary untuk heuristic navigasi.
_NAV_TOKEN_RE = re.compile(
    r"\b(?:prev(?:ious)?|next|index|toc|home|main(?:\s*page)?|"
    r"table\s*of\s*contents|chapter\s*list)\b",
    re.IGNORECASE,
)


def _looks_like_nav_line(line: str) -> bool:
    """Heuristic: baris pendek yang berisi >=2 token navigasi pasti adalah link bar.

    Contoh yang tertangkap:
        "<< Previous | Index | Next >>"
        "Prev | TOC | Next"
        "[Prev] [TOC] [Next]"
    """
    s = line.strip()
    if not s or len(s) > 120:
        return False
    matches = _NAV_TOKEN_RE.findall(s)
    return len(matches) >= 2


# A. Kredit & tanda tangan translator
PATTERNS_CREDITS: list[Matcher] = [
    _line(r"(?:translated|translation|trans|tl(?:'d)?|tl(?:ed)?)\s*(?:by|:)\s*.+"),
    _line(r"translator\s*[:\-]\s*.+"),
    _line(r"(?:edited|edit|editor|ed)\s*(?:by|:)\s*.+"),
    _line(r"(?:proof[\- ]?read|proofreader|pr)\s*(?:by|:)\s*.+"),
    _line(r"(?:checked|qc|quality\s*check)\s*(?:by|:)\s*.+"),
    _line(r"raw\s*provider\s*[:\-]\s*.+"),
    _line(r"original(?:\s*author)?\s*[:\-]\s*.+"),
    _line(r"(?:source|raw|original\s*source)\s*[:\-]\s*.+"),
    _line(r"posted\s*on\s*.+"),
    _line(r"read\s*(?:more|the\s*latest|advanced?\s*chapters?)\s*(?:at|on)\s*.+"),
    _line(r"(?:visit|check\s*out)\s*(?:our|us|me|my)?\s*(?:site|website|blog)\s*(?:at|on)?\s*.+"),
    _line(r"this\s*chapter\s*is\s*(?:translated|brought\s*to\s*you)\s*by\s*.+"),
    _line(r"(?:t/?n|tn|tl/?n|trans\s*note|translator['\u2019]?s?\s*note)\s*[:\-].*translation\s*team.*"),
    # Site attribution boilerplate
    _line(r"(?:please\s*)?(?:read|continue\s*reading)\s*(?:this\s*chapter\s*)?(?:at|on|from)\s*\S+"),
    _line(r"this\s*translation\s*belongs\s*to\s*.+"),
]

# B. Donasi / dukung translator
PATTERNS_DONATE: list[Matcher] = [
    _line(r".*\bpatreon(?:\.com)?\b.*"),
    _line(r".*\bko[\- ]?fi(?:\.com)?\b.*"),
    _line(r".*\bbuy\s*me\s*a\s*coffee\b.*"),
    _line(r".*\bbmac\b.*"),
    _line(r".*\bpaypal(?:\.me)?\b.*"),
    _line(r".*\bdonate\s*(?:via|here|now|to)\b.*"),
    _line(r".*\bsupport\s*(?:me|us|the\s*translation|on\s*patreon)\b.*"),
    _line(r".*\bbecome\s*(?:a|my)\s*patron\b.*"),
    _line(r".*\btip\s*jar\b.*"),
    _line(r".*\bsend\s*(?:a\s*)?tip\b.*"),
    _line(r".*\bgcash\b.*"),
    _line(r".*\bsaweria(?:\.co)?\b.*"),  # ID donation platform
    _line(r".*\btrakteer(?:\.id)?\b.*"),  # ID donation platform
]

# C. Discord / sosmed / komunitas
PATTERNS_SOCIAL: list[Matcher] = [
    _line(r".*\bdiscord(?:\.gg|\.com)?\b.*"),
    _line(r".*\bjoin\s*(?:our|the|my)?\s*discord\b.*"),
    _line(r".*\bt\.me/\S+.*"),
    _line(r".*\btelegram\s*(?:channel|group|chat)\b.*"),
    _line(r".*\b(?:follow|join)\s*(?:us|me)\s*on\s*(?:twitter|x|instagram|ig|facebook|fb|youtube|tiktok|reddit)\b.*"),
    _line(r".*\b(?:our|my)\s*(?:twitter|instagram|facebook|youtube)\s*(?:account|page|channel)\b.*"),
]

# D. Navigasi chapter & garis pemisah polos
PATTERNS_NAVIGATION: list[Matcher] = [
    # Multi-token nav line: "<< Previous | Index | Next >>", "Prev | TOC | Next"
    _looks_like_nav_line,
    # Single nav phrase
    _line(r"(?:back\s*to\s*)?(?:table\s*of\s*contents|toc|index|chapter\s*list)"),
    _line(r"(?:return|back|go)\s*to\s*(?:main(?:\s*page)?|home(?:\s*page)?|index|toc).*"),
    _line(r"<<\s*prev(?:ious)?(?:\s*chapter)?\s*"),
    _line(r"\u2190\s*prev(?:ious)?(?:\s*chapter)?\s*"),
    _line(r"\s*next(?:\s*chapter)?\s*>>\s*"),
    _line(r"\s*next(?:\s*chapter)?\s*\u2192\s*"),
    _line(r"\[\s*(?:prev(?:ious)?|next|toc|index)\s*\]"),
    # Garis pemisah polos
    _line(r"[=\-_*~\u2014\u2013]{3,}"),
    # Repeated chapter title boilerplate sering ada di header/footer scrape
    _line(r"chapter\s*\d+\s*[:\-]?\s*(?:end|fin|to\s*be\s*continued)"),
    _line(r"to\s*be\s*continued\.?\.?\.?"),
    _line(r"\[?(?:end|fin)\s*of\s*chapter\s*\d+\]?"),
]

# E. Iklan / engagement
PATTERNS_ADS: list[Matcher] = [
    _line(r".*\b(?:like\s*and\s*(?:comment|subscribe)|don['\u2019]?t\s*forget\s*to\s*(?:like|subscribe|comment))\b.*"),
    _line(r".*\b(?:subscribe|hit\s*the\s*bell|smash\s*that\s*like)\b.*"),
    _line(r".*\bleave\s*a\s*(?:comment|review|rating|like)\b.*"),
    _line(r".*\brate\s*this\s*(?:novel|chapter|story)\b.*"),
    _line(r".*\bshare\s*(?:this\s*chapter|with\s*your\s*friends)\b.*"),
    _line(r".*\bbookmark\s*(?:this|us|the\s*site)\b.*"),
    _line(r".*\badd\s*(?:this|us|the)\s*(?:novel\s*)?to\s*(?:your\s*)?(?:nu\s*)?(?:reading\s*list|library|favorites?)\b.*"),
    _line(r".*\bvote\s*(?:for\s*us|on\s*novel\s*updates|on\s*nu)\b.*"),
    _line(r".*\bif\s*you\s*(?:enjoyed|liked)\s*(?:this|the)\s*(?:chapter|story).*"),
]

# F. Jadwal rilis & sponsorship
PATTERNS_SCHEDULE: list[Matcher] = [
    _line(r".*\bsponsored\s*(?:chapter|by)\b.*"),
    _line(r".*\bthanks?\s*(?:to|goes?\s*to)\s*.+\s*(?:for\s*sponsoring|for\s*the\s*sponsor)\b.*"),
    _line(r".*\bbonus\s*chapter\b.*"),
    _line(r".*\bmass\s*release\b.*"),
    _line(r".*\b(?:this\s*week['\u2019]?s|today['\u2019]?s)\s*(?:release|chapter)\s*schedule\b.*"),
    _line(r".*\brelease\s*schedule\s*[:\-].*"),
    _line(r".*\bchapter\s*\d+\s*of\s*\d+\b.*"),
    _line(r".*\b(?:queue|backlog)\s*[:\-]\s*\d+.*"),
    _line(r".*\b(?:goal|target)\s*[:\-]\s*.+\s*(?:patrons?|patreon|chapter)s?.*"),
]

# G. Author / Translator notes (DEFAULT KEEP)
# Catatan: prefix yang sangat eksplisit saja, supaya gak nge-strip narasi cerita.
PATTERNS_TL_NOTES: list[Matcher] = [
    _line(r"(?:t/?n|tn|tl/?n|tl\s*note|trans(?:lator)?['\u2019]?s?\s*note)\s*[:\-].*"),
    _line(r"(?:a/?n|an|author['\u2019]?s?\s*note)\s*[:\-].*"),
    _line(r"(?:e/?n|editor['\u2019]?s?\s*note)\s*[:\-].*"),
    _line(r"\[\s*(?:tl?n|an|en)\s*[:\-].*\]"),
    _line(r"\(\s*(?:tl?n|an|en)\s*[:\-].*\)"),
    _line(r"\*+\s*(?:tl?n|an|en)\s*[:\-].*\*+"),
    # Footnote-style: ¹, ², (1), [1] dst — too aggressive untuk default;
    # biarin TL notes prefix saja yang ditangkap.
]

# H. Residu HTML scraping (substring replacement)
HTML_RESIDUE_SUBS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"&nbsp;"), " "),
    (re.compile(r"&amp;"), "&"),
    (re.compile(r"&quot;"), '"'),
    (re.compile(r"&apos;"), "'"),
    (re.compile(r"&#39;"), "'"),
    (re.compile(r"&lt;"), "<"),
    (re.compile(r"&gt;"), ">"),
    (re.compile(r"&hellip;"), "…"),
    (re.compile(r"&mdash;"), "—"),
    (re.compile(r"&ndash;"), "–"),
    (re.compile(r"</?p\s*[^>]*>", re.IGNORECASE), ""),
    (re.compile(r"</?br\s*/?\s*>", re.IGNORECASE), "\n"),
    (re.compile(r"</?span\s*[^>]*>", re.IGNORECASE), ""),
    (re.compile(r"</?div\s*[^>]*>", re.IGNORECASE), ""),
    (re.compile(r"</?em\s*[^>]*>", re.IGNORECASE), "*"),
    (re.compile(r"</?strong\s*[^>]*>", re.IGNORECASE), "**"),
    (re.compile(r"</?i\s*[^>]*>", re.IGNORECASE), "*"),
    (re.compile(r"</?b\s*[^>]*>", re.IGNORECASE), "**"),
    (re.compile(r"</?u\s*[^>]*>", re.IGNORECASE), ""),
    # Hapus comment HTML
    (re.compile(r"<!--.*?-->", re.DOTALL), ""),
    # Hapus tag generic lain
    (re.compile(r"</?[a-z][a-z0-9]*\s*[^>]*>", re.IGNORECASE), ""),
]

# I. Footer / watermark
PATTERNS_FOOTER: list[Matcher] = [
    _line(r".*\ball\s*rights\s*reserved\b.*"),
    _line(r"(?:©|\(c\)|copyright)\s*\d{4}.*"),
    _line(r".*\bdo\s*not\s*(?:re-?post|copy|reproduce)\b.*"),
    _line(r".*\bunauthorized\s*(?:translation|copying|reproduction)\b.*"),
    _line(r".*\bfor\s*free\s*reading\s*only\s*(?:at|on)\s*.+"),
    _line(r".*\bif\s*you\s*(?:see|are\s*reading)\s*this\s*(?:on|at|elsewhere)\b.*"),
    _line(r".*\bthis\s*translation\s*is\s*hosted\s*(?:exclusively\s*)?(?:at|on)\s*.+"),
    _line(r".*\bplease\s*(?:read|support)\s*(?:the\s*)?(?:original|translator)\b.*"),
    # URL telanjang sebagai 1 baris penuh (sering watermark)
    _line(r"https?://\S+"),
    _line(r"www\.\S+\.\S+"),
]


# Tambahan: pola NovelUpdates-flavored (link & boilerplate yang sering muncul
# di chapter yang di-scrape dari site translator yang terdaftar di NU)
PATTERNS_NOVELUPDATES: list[Matcher] = [
    _line(r".*\bnovel\s*updates?\b.*"),
    _line(r".*\bnu\.com\b.*"),
    _line(r".*\bvote\s*(?:for\s*us|this\s*novel)\s*on\s*(?:nu|novel\s*updates?)\b.*"),
    _line(r".*\badd\s*(?:this|the)\s*(?:novel\s*)?to\s*(?:your\s*)?(?:nu\s*)?reading\s*list\b.*"),
    _line(r".*\bcheck\s*(?:out\s*)?(?:my\s*|our\s*)?other\s*(?:novels|translations|projects)\s*(?:on|at)\s*nu\b.*"),
]


# ----------------------------------------------------------------------
# Whitespace normalization (J)
# ----------------------------------------------------------------------

def _normalize_whitespace(text: str) -> str:
    # Strip trailing whitespace per baris
    lines = [ln.rstrip() for ln in text.split("\n")]
    # Replace tab dengan 4 spasi
    lines = [ln.replace("\t", "    ") for ln in lines]
    text = "\n".join(lines)
    # Multiple blank lines (>2 newlines berturut) -> 2 newlines (1 blank line)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip leading & trailing blank lines untuk hasil akhir
    return text.strip("\n") + "\n"


# ----------------------------------------------------------------------
# FilterEngine
# ----------------------------------------------------------------------

CATEGORIES = ("credits", "donate", "social", "navigation", "ads",
              "schedule", "tl_notes", "html_residue", "footer", "whitespace",
              "novelupdates")

CATEGORY_PATTERNS: dict[str, list[Matcher]] = {
    "credits": PATTERNS_CREDITS,
    "donate": PATTERNS_DONATE,
    "social": PATTERNS_SOCIAL,
    "navigation": PATTERNS_NAVIGATION,
    "ads": PATTERNS_ADS,
    "schedule": PATTERNS_SCHEDULE,
    "tl_notes": PATTERNS_TL_NOTES,
    "footer": PATTERNS_FOOTER,
    "novelupdates": PATTERNS_NOVELUPDATES,
}

# Default kategori yang aktif (G/tl_notes default OFF supaya konteks cerita
# yang kadang ada di TL note tidak hilang)
DEFAULT_ENABLED: tuple[str, ...] = (
    "credits", "donate", "social", "navigation", "ads",
    "schedule", "html_residue", "footer", "whitespace", "novelupdates",
)


@dataclass
class FilterStats:
    """Statistik per kategori untuk transparansi/dry-run."""
    removed_lines: dict[str, int] = field(default_factory=dict)
    html_substitutions: int = 0
    total_lines_in: int = 0
    total_lines_out: int = 0

    def total_removed(self) -> int:
        return sum(self.removed_lines.values())


@dataclass
class FilterEngine:
    enabled: tuple[str, ...] = DEFAULT_ENABLED
    custom_patterns: list[Matcher] = field(default_factory=list)

    @classmethod
    def from_config(
        cls,
        cfg_filters: dict | None,
        custom_patterns_path: Path | None = None,
    ) -> "FilterEngine":
        """
        Bangun FilterEngine dari blok 'filters' di config.yaml.

        Format yang diharapkan di config:
            filters:
              enabled: true                 # global on/off
              categories:
                credits: true
                donate: true
                ...
              custom_patterns:              # daftar regex inline
                - "^=+$"
        """
        cfg_filters = cfg_filters or {}
        if not cfg_filters.get("enabled", True):
            # Disabled total: kosong
            return cls(enabled=(), custom_patterns=[])

        cats_cfg = cfg_filters.get("categories", {}) or {}
        enabled: list[str] = []
        for cat in CATEGORIES:
            # Default berdasarkan DEFAULT_ENABLED kalau tidak di-set
            default_on = cat in DEFAULT_ENABLED
            if cats_cfg.get(cat, default_on):
                enabled.append(cat)

        # Custom patterns di-anchor ke seluruh BARIS (sesuai dokumentasi di
        # config.yaml). Pakai _line() — bukan re.compile().match — supaya
        # pola seperti `translated` tidak menghapus baris yang kebetulan
        # diawali kata itu (mis. "translated version of the story...").
        custom: list[Matcher] = []
        for pat_str in (cfg_filters.get("custom_patterns") or []):
            try:
                custom.append(_line(pat_str))
            except re.error:
                # Kalau regex invalid, skip diam-diam (akan log di translate.py)
                pass

        # Per-novel filters file (1 regex per baris, # untuk komentar).
        if custom_patterns_path and custom_patterns_path.exists():
            for raw in custom_patterns_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    custom.append(_line(line))
                except re.error:
                    pass

        return cls(enabled=tuple(enabled), custom_patterns=custom)

    # ------------------------------------------------------------------
    # Cleaning
    # ------------------------------------------------------------------

    def _line_match_category(self, line: str) -> str | None:
        """Return category name kalau line match salah satu pola aktif."""
        # Custom patterns dianggap kategori "custom"
        for matcher in self.custom_patterns:
            if matcher(line):
                return "custom"
        for cat in self.enabled:
            for matcher in CATEGORY_PATTERNS.get(cat, []):
                if matcher(line):
                    return cat
        return None

    def clean(self, text: str) -> tuple[str, FilterStats]:
        """Bersihkan text dan return (text_bersih, stats)."""
        stats = FilterStats()

        # 1. HTML residue (substring replacement) — dilakukan dulu supaya
        #    line-level matcher melihat baris yang sudah bersih dari tag.
        if "html_residue" in self.enabled:
            for pat, repl in HTML_RESIDUE_SUBS:
                text, n = pat.subn(repl, text)
                stats.html_substitutions += n

        # 2. Line-level removal
        lines = text.split("\n")
        stats.total_lines_in = len(lines)
        kept: list[str] = []
        for ln in lines:
            cat = self._line_match_category(ln)
            if cat is None:
                kept.append(ln)
            else:
                stats.removed_lines[cat] = stats.removed_lines.get(cat, 0) + 1
        text = "\n".join(kept)

        # 3. Whitespace normalization
        if "whitespace" in self.enabled:
            text = _normalize_whitespace(text)

        stats.total_lines_out = len(text.split("\n"))
        return text, stats

    # ------------------------------------------------------------------
    # Dry-run: tunjukkan baris apa yang akan dihapus tanpa benar-benar
    # menghapus. Berguna untuk preview & tuning regex per-novel.
    # ------------------------------------------------------------------

    def dry_run(self, text: str) -> list[tuple[int, str, str]]:
        """Return list of (line_number_1based, line_content, category)."""
        # Apply HTML residue dulu kalau aktif (sesuai clean())
        if "html_residue" in self.enabled:
            for pat, repl in HTML_RESIDUE_SUBS:
                text = pat.sub(repl, text)

        out: list[tuple[int, str, str]] = []
        for i, ln in enumerate(text.split("\n"), 1):
            cat = self._line_match_category(ln)
            if cat is not None:
                out.append((i, ln, cat))
        return out


# ----------------------------------------------------------------------
# Convenience fungsi top-level
# ----------------------------------------------------------------------

def clean_text(
    text: str,
    *,
    enabled: tuple[str, ...] = DEFAULT_ENABLED,
    custom_patterns: list[str] | None = None,
) -> tuple[str, FilterStats]:
    """Helper untuk pemakaian sederhana tanpa instansiasi engine."""
    custom_compiled: list[Matcher] = []
    for pat_str in (custom_patterns or []):
        try:
            custom_compiled.append(_line(pat_str))
        except re.error:
            pass
    engine = FilterEngine(enabled=enabled, custom_patterns=custom_compiled)
    return engine.clean(text)
