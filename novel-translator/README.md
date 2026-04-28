# Novel Translator

Script Python untuk menerjemahkan chapter novel (file `.txt`) dari **Bahasa Inggris**, **Jepang**, **Korea**, atau **Mandarin** ke **Bahasa Indonesia**. Mendukung **tiga engine LLM**:

- **Google Gemini** (free tier, default) — `gemini-2.5-flash`
- **Runeria** (OpenAI-compatible gateway, https://runeria.fun) — `claude-sonnet-4`, `claude-haiku-4.5`
- **Cline** (https://app.cline.bot) — `moonshotai/kimi-k2.6`, `minimax/minimax-m2.5`, `z-ai/glm-5`, dll

**Tidak hafal command? Jalan tanpa argumen** → muncul menu interaktif yang nuntun kamu langkah-langkah:
```bash
python translate.py     # menu interaktif
```

Fitur utama:
- 1 file `.txt` per chapter (in & out)
- Auto-detect bahasa sumber: **EN / JP / KR / CN**
- **Multi-engine**: ganti antar provider via `engine:` di config.yaml atau `--engine` CLI
- **Glossary per-novel** (auto-extract pakai LLM, sekali setup) → nama karakter & istilah konsisten antar chapter
- **Resume** otomatis: chapter yang sudah selesai di-skip, yang gagal bisa diulang
- **Chunking otomatis** untuk chapter panjang
- **Rate limiting + retry** untuk free tier (10 RPM)
- **Tanpa sensor** (safety_settings BLOCK_NONE) — aman untuk konten 18+
- **Default kasual `aku/kamu`** untuk dialog & POV-1, dengan post-processing opsional untuk membersihkan sisa `Anda/Saya`
- **Filter boilerplate otomatis** — hapus kredit translator, link Patreon/Discord, navigasi prev/next, watermark, residu HTML, dll sebelum dikirim ke Gemini (hemat token + output bersih)
- Style: natural Bahasa Indonesia tanpa mengubah struktur kalimat/paragraf

---

## 1. Setup (sekali saja)

```bash
# 1. Pasang dependency
pip install -r requirements.txt

# 2a. Pakai Gemini (default — gratis di https://aistudio.google.com/apikey)
export GEMINI_API_KEY="paste_api_key_kamu_di_sini"

# 2b. ATAU pakai Runeria
export RUNERIA_API_KEY="paste_api_key_runeria"

# 2c. ATAU pakai Cline
export CLINE_API_KEY="paste_api_key_cline"
```

> **Tip:** Tambahkan baris `export ..._API_KEY=...` ke `~/.bashrc` atau `~/.zshrc` agar tidak perlu set ulang setiap kali buka terminal.
>
> Alternatif: isi `<engine>.api_key` di `config.yaml`. Tapi env var lebih aman (jangan sampai key ke-commit).

### Menu interaktif (paling gampang)

Kalau bingung dengan banyak command, jalanin saja:
```bash
python translate.py
```
Atau eksplisit:
```bash
python translate.py --menu
```
Muncul menu seperti ini:
```
======================================
 Novel Translator — menu interaktif
 engine=gemini  model=gemini-2.5-flash
======================================
  1. List semua novel & status
  2. Translate novel
  3. Edit glossary
  4. Ganti engine & model
  5. Build glossary saja (tanpa translate)
  6. Dry-run filter (preview baris yang dihapus)
  7. Cheatsheet command CLI
  0. Keluar
```
Tiap pilihan nuntun kamu pilih novel, range chapter, dst tanpa harus hafal flag. Pilihan **4** bisa simpan engine+model permanen ke `config.yaml` (atau session-only).

CLI tetap berfungsi penuh (untuk skrip/automation). Menu cuma layer di atasnya.

### Pilih engine LLM

```yaml
# config.yaml
engine: "gemini"     # default. Pilihan: "gemini" | "runeria" | "cline"
```

Atau override sekali pakai dari CLI:
```bash
python translate.py --novel my_novel --engine runeria
python translate.py --novel my_novel --engine cline
```

Atau ganti via menu interaktif (pilihan 4) — bisa simpan permanen ke config.

**Kapan pakai apa:**
- **Gemini free tier** — gratis, 500 req/hari (`gemini-2.5-flash`). Bagus untuk volume besar.
- **Runeria + claude-sonnet-4** — kualitas terbaik untuk dialog & POV halus, dipilih kalau Gemini quota habis.
- **Cline + kimi-k2.6** — alternatif Asia-leaning, terutama untuk source CN/JP yang panjang.

**Model Runeria yang tersedia di plan basic:**

| Model | Catatan |
|-------|---------|
| `claude-sonnet-4` | TERBAIK. Naturally pilih "aku/kamu" tanpa dipaksa, konsisten di chapter panjang. **Default**. |
| `claude-haiku-4.5` | Lebih cepat & murah dari sonnet, sedikit di bawah untuk dialog halus. Bagus untuk batch besar. |

Model lain (`deepseek-3.2`, `glm-5`, `minimax-m2.5`, dll) memerlukan plan **Pro/Enterprise** — basic plan akan dapat HTTP 403 `model_not_allowed`. Script fail-fast (tidak retry sia-sia).

**Model Cline yang direkomendasikan:**

| Model | Catatan |
|-------|---------|
| `moonshotai/kimi-k2.6` | **Default**. Kimi kuat untuk source CN/JP & dialog panjang. |
| `minimax/minimax-m2.5` | Decent dialog. |
| `z-ai/glm-5` | Solid CN→ID, mid-tier. |
| `kwaipilot/kat-coder-pro` | **Hindari** — coder model. |

**Kalau provider balas HTTP 503** (server overload sementara), script otomatis retry dengan exponential backoff (5→10→20→40→80 detik). Kalau habis 5 attempt masih 503, tunggu beberapa menit & ulang.

---

## 2. Struktur Folder

```
novel-translator/
├── translate.py
├── config.yaml
├── requirements.txt
├── prompts/
│   ├── translate_en.txt
│   ├── translate_jp.txt
│   ├── translate_kr.txt
│   ├── translate_cn.txt
│   └── extract_glossary.txt
├── filters.py                   ← modul filter boilerplate
├── novels/
│   └── <nama_novel>/
│       ├── source/             ← KAMU letakkan chapter .txt asli di sini
│       │   ├── Chapter_001.txt
│       │   ├── Chapter_002.txt
│       │   └── ...
│       ├── translated/         ← hasil terjemahan otomatis muncul di sini
│       ├── glossary.json       ← glossary novel ini (auto-generate / edit manual)
│       ├── filters.txt         ← (opsional) regex filter custom per-novel
│       └── .progress           ← state (jangan diubah manual)
└── logs/<nama_novel>.log
```

**Penting**: nama file chapter sebaiknya berurutan secara natural, misal `Chapter_001.txt`, `Chapter_002.txt`, dst — script sudah handle natural sort jadi `Chapter_2` muncul sebelum `Chapter_10`.

---

## 3. Cara Pakai

### A. Pakai cepat (auto, paling simpel)

```bash
# 1. Buat folder novel & masukkan chapter .txt
mkdir -p novels/my_novel/source
cp /path/ke/chapter/*.txt novels/my_novel/source/

# 2. Jalankan
python translate.py --novel my_novel
```

Pertama kali jalan, script akan:
1. Auto-detect bahasa dari chapter pertama
2. Build glossary otomatis dari 3 chapter awal (sekali saja)
3. Mulai menerjemahkan satu per satu

Hasil keluar di `novels/my_novel/translated/`.

### B. Build glossary dulu, review, baru terjemahkan

Ini cara terbaik untuk hasil paling konsisten:

```bash
# Build glossary saja, lalu berhenti
python translate.py --novel my_novel --build-glossary

# Buka novels/my_novel/glossary.json, review nama karakter / istilah,
# perbaiki kalau ada yang salah (contoh: "Yukine" mungkin lebih bagus jadi "Yukino")

# Lalu jalankan terjemahan
python translate.py --novel my_novel
```

### C. Range chapter tertentu

```bash
python translate.py --novel my_novel --only 1-10
python translate.py --novel my_novel --only 1,3,5-8,12
```

### D. Paksa bahasa sumber (override auto-detect)

```bash
python translate.py --novel my_novel --lang jp
python translate.py --novel my_novel --lang en
python translate.py --novel my_novel --lang kr
python translate.py --novel my_novel --lang cn
```

Auto-detect bekerja berdasarkan rasio karakter:
- Hangul (한글) → `kr`
- Hiragana / Katakana (ひらがな / カタカナ) → `jp`
- Hanzi tanpa kana/hangul → `cn`
- Sisanya → `en`

### E. Timpa hasil terjemahan yang sudah ada

```bash
python translate.py --novel my_novel --rebuild
python translate.py --novel my_novel --only 5 --rebuild  # rebuild chapter 5 saja
```

### F. Lihat daftar novel & status

```bash
python translate.py --list
```

### G. Preview apa saja yang akan dihapus filter (tanpa terjemahkan)

```bash
python translate.py --novel my_novel --dry-run-filter
```

Output contoh:
```
[Chapter_001.txt] 11 baris akan dihapus:
  L3    [credits]    Translated by Lucky7 | Edited by Owl
  L4    [credits]    Source: lightnovelpub.com
  L5    [credits]    Read advanced chapters at patreon.com/lucky7
  L6    [social]     Join our Discord at discord.gg/lucky7
  L8    [navigation] << Previous | Index | Next >>
  L18   [ads]        If you enjoyed this chapter, please leave a comment.
  L20   [novelupdates] Vote this novel on NovelUpdates if you like it!
  L22   [navigation] —————
  L24   [navigation] To be continued...
  L26   [footer]     © 2024 Lucky7 Translations. All rights reserved.
  L27   [footer]     Do not repost without permission.
```

Kalau ada baris yang **harusnya tidak dihapus** (false positive) atau ada boilerplate khusus situsmu yang tidak tertangkap, sesuaikan `filters.*` di `config.yaml` atau buat `novels/<nama>/filters.txt`.

Output contoh:
```
my_novel                       source= 120  translated=  87  glossary=yes
isekai_hero                    source=  45  translated=   0  glossary=no
```

---

## 4. Edit Glossary

### A. Lewat CLI (rekomendasi — tanpa edit JSON manual)

Glossary editor jalan **offline** — tidak butuh API key, tidak konsumsi quota.

```bash
# Lihat isi glossary
python translate.py --novel my_novel --glossary-list

# Tambah entry. Tipe: character | place | term
python translate.py --novel my_novel --glossary-add character "Yukine" "Yukino"
python translate.py --novel my_novel --glossary-add place     "Shibuya" "Distrik Shibuya"
python translate.py --novel my_novel --glossary-add term      "Reiatsu" "Reiatsu"

# Update entry yang sudah ada (overwrite target)
python translate.py --novel my_novel --glossary-edit character "Yukine" "Yukino-chan"

# Hapus entry
python translate.py --novel my_novel --glossary-remove character "Yukine"

# Set / hapus style notes
python translate.py --novel my_novel --glossary-set-style "Pakai honorifik Jepang."
python translate.py --novel my_novel --glossary-set-style ""
```

### B. Auto-update glossary saat translate chapter baru

Set `glossary.auto_update_every: N` di `config.yaml` (default 20). Setiap N chapter berhasil diterjemahkan, script otomatis scan N chapter source terbaru untuk **nama/istilah baru** dan menambahkannya ke `glossary.json` secara non-destructive (entry yang sudah kamu edit manual **tidak ditimpa**).

Berguna untuk novel panjang di mana karakter baru muncul jauh setelah chapter awal. Tiap auto-update memanggil Gemini sekali — kalau kuota ketat, set angkanya lebih besar (mis. 50) atau set 0 untuk disabled.

```yaml
glossary:
  auto_update_every: 20  # 0 = disabled
```

### C. Edit Manual (tetap didukung)

Kalau lebih nyaman edit JSON langsung di `novels/<nama>/glossary.json`:

```json
{
  "characters": {
    "田中太郎": "Tanaka Tarou",
    "雪音": "Yukino"
  },
  "places": {
    "Akihabara": "Akihabara"
  },
  "terms": {
    "魔力": "Mana",
    "勇者": "Hero",
    "ステータス": "Status"
  },
  "style_notes": "POV1, narasi santai, gaya light novel modern. Sufiks -san/-kun/-chan dipertahankan."
}
```

Format: `"bentuk asli di teks": "bentuk yang dipakai di terjemahan"`.

Setelah edit, jalan lagi `python translate.py --novel my_novel` — glossary baru otomatis dipakai untuk chapter berikutnya. Untuk chapter yang sudah diterjemahkan, pakai `--rebuild`.

> Tips: kalau merge glossary dari multiple kali extract, **entry yang sudah ada tidak akan ditimpa** — jadi aman untuk edit manual & build ulang.

---

## 5. Konfigurasi (config.yaml)

| Setting | Default | Keterangan |
|---|---|---|
| `gemini.model` | `gemini-2.5-flash` | Model untuk translate. Free tier paling besar quotanya. |
| `gemini.glossary_model` | `gemini-2.5-flash` | Model untuk extract glossary. |
| `gemini.temperature` | `0.3` | Rendah = konsisten, tinggi = kreatif. |
| `gemini.requests_per_minute` | `8` | Rate limit. Free tier flash = 10 RPM, kita set 8 biar aman. |
| `gemini.max_retries` | `5` | Retry kalau error / quota habis. |
| `translation.max_chars_per_chunk` | `8000` | Pecah chapter kalau lebih panjang dari ini. |
| `glossary.mode` | `auto` | `auto` / `manual` / `skip` |
| `glossary.sample_chapters` | `3` | Berapa chapter awal untuk auto-extract. |
| `glossary.auto_update_every` | `20` | Update glossary setiap N chapter selesai (nama baru di-merge ke `glossary.json` secara non-destructive). 0 = disabled. |
| `filters.enabled` | `true` | Master toggle filter boilerplate. |
| `filters.apply_pre_translation` | `true` | Filter source SEBELUM dikirim ke Gemini (hemat token). |
| `filters.apply_post_translation` | `true` | Filter hasil terjemahan (pengaman lapis kedua). |
| `filters.categories.<nama>` | `true` (kecuali `tl_notes` = `false`) | Toggle per-kategori. Lihat detail di `config.yaml`. |
| `filters.custom_patterns` | `[]` | Regex tambahan global (case-insensitive, anchor ke seluruh baris). |
| `post_process.normalize_pronouns` | `false` | Aktifkan pembersih `Anda/Saya` → `kamu/aku` setelah Gemini menerjemahkan. |
| `post_process.normalize_pronouns_strength` | `safe` | `safe` (skip paragraf yang mengandung penanda formal seperti "Yang Mulia", "Tuan ", "Shifu", dll) atau `aggressive` (ganti semua tanpa kecuali). |

Kalau Gemini free tier mulai sering 429 (quota harian habis), turunkan `requests_per_minute` ke 5, atau ganti `model` ke `gemini-2.5-flash-lite` (quota lebih besar tapi kualitas sedikit di bawah).

---

## 6. Free Tier Gemini — Catatan Penting

Per April 2026, free tier API Gemini kira-kira:

| Model | RPM | TPM | RPD |
|---|---|---|---|
| `gemini-2.5-flash` | 10 | 250rb | 500 |
| `gemini-2.5-flash-lite` | 15 | 250rb | 1000 |
| `gemini-2.5-pro` | 5 | 250rb | 100 |

> RPM = request/menit, TPM = token/menit, RPD = request/hari.

Untuk 1 chapter ≈ 1 request, jadi free tier flash bisa terjemahkan **~500 chapter per hari**. Sangat cukup. Kalau habis, tunggu 24 jam atau ganti API key.

Cek limit terbaru di: https://ai.google.dev/gemini-api/docs/rate-limits

---

## 7. Troubleshooting

**"Prompt diblokir Gemini"**
Konten dinilai sensitif walau `safety_settings=BLOCK_NONE`. Hal ini jarang terjadi tapi bisa untuk konten ekstrem. Workaround: pecah chapter manual atau ganti model.

**"429 quota exceeded"**
Free tier habis. Tunggu reset (24 jam) atau buat API key baru.

**Hasil ada bagian yang terpotong**
Naikkan `gemini.max_output_tokens` di config (default 16384, bisa sampai 65536 untuk model 2.5).

**Nama karakter tidak konsisten antar chapter**
Build glossary lebih komprehensif: `python translate.py --novel X --build-glossary` setelah ada banyak chapter, lalu edit manual file glossary.json.

**Hasil masih sering pakai "Anda/Saya" padahal kontekstnya kasual**
Ini adalah problem klasik karena Gemini bias ke bentuk formal. Ada dua pendekatan:

1. (Rekomendasi) Prompt sudah ditegaskan untuk default `aku/kamu`. Pastikan kamu pakai versi terbaru `prompts/translate_*.txt` di repo.
2. Aktifkan post-processing pembersih di `config.yaml`:
   ```yaml
   post_process:
     normalize_pronouns: true
     normalize_pronouns_strength: "safe"   # safe | aggressive
   ```
   - Mode `safe`: paragraf yang mengandung penanda formal (`Yang Mulia`, `Tuan `, `Nyonya`, `Jenderal`, `Komandan`, `Hamba`, `Shifu`, `Senpai`, dst) **tidak ikut diubah** — jadi dialog formal tetap aman.
   - Mode `aggressive`: ganti SEMUA `Anda/Saya` → `kamu/aku` tanpa kecuali. Cuma pakai kalau yakin novelmu tidak punya konteks formal.

**Mau ubah style ke "lo/gue" atau gaya webnovel**
Edit file `prompts/translate_<lang>.txt` di bagian "KATA GANTI ORANG". Tiap bahasa punya prompt sendiri (`translate_en.txt`, `translate_jp.txt`, `translate_kr.txt`, `translate_cn.txt`).

**Hasil masih ada baris kredit translator / link Patreon / Discord / dll yang ikut diterjemahkan**
Filter boilerplate sudah aktif default (kategori `credits`, `donate`, `social`, `navigation`, `ads`, `schedule`, `html_residue`, `footer`, `whitespace`, `novelupdates`). Kalau site sumbermu punya boilerplate spesifik yang tidak tertangkap:

1. Lihat dulu apa yang AKAN dihapus pakai `--dry-run-filter`:
   ```bash
   python translate.py --novel my_novel --dry-run-filter
   ```
   Ini hanya menampilkan baris yang match filter, tanpa menerjemahkan apa pun.
2. Tambahkan regex custom di salah satu tempat:
   - **Untuk semua novel**: `config.yaml` → `filters.custom_patterns` (list regex).
   - **Per-novel**: buat file `novels/<nama_novel>/filters.txt`, satu regex per baris (komentar `#` & blank line OK). Contoh:
     ```
     # Hapus baris kredit khusus situs aku
     ^translated\s+by\s+ardanalf\b.*
     ^https?://ardanalfino\.my\.id/.*
     ```
3. Mau matikan kategori tertentu? Set `false` di `config.yaml` → `filters.categories.<nama>`.
4. Mau matikan filter total? `filters.enabled: false`.

---

## 8. Ringkasan Workflow Harian

```bash
# Update API key kalau perlu
export GEMINI_API_KEY="..."

# Tambah chapter baru ke novels/my_novel/source/
# Lalu:
python translate.py --novel my_novel

# Selesai. Hasil di novels/my_novel/translated/
```

Selamat menerjemahkan!
