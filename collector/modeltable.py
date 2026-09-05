"""Pembaca tabel harga per-model pada halaman harga API.

Kenapa ini ada
--------------
Halaman yang harganya PALING sering berubah adalah halaman harga API
(OpenAI, DeepInfra, Voyage, Together, Fireworks, ...). Tapi sampai 04/09/2026
perubahan di sana selalu jatuh ke `catalog_change`, karena heuristik kartu
membaca nama model sebagai "nama paket": `gpt-6-astra` tercatat sebagai paket
baru, `o4-mini-2025-04-16` sebagai paket hilang. Padahal yang terjadi adalah
peristiwa harga yang sah dan justru paling berharga untuk arsip.

Bedanya dengan heuristik kartu: di sini strukturnya sudah eksplisit di HTML —
`<table>` dengan kolom pertama "Model" dan kolom-kolom harga. Itu jauh lebih
stabil antar hari daripada menebak batas kartu dari posisi teks harga, jadi
`model_price_changed` boleh dipercaya sebagai perubahan harga sungguhan.

Modul ini sengaja umum, bukan adapter per-situs. Tabel harga API punya bentuk
yang seragam, dan daftar adapter per-slug akan langsung basi begitu ada vendor
baru.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# Kolom pertama harus benar-benar menyatakan "ini baris model". Tanpa syarat
# ini, tabel perbandingan paket ikut tertangkap: header mistral berbunyi
# ['', 'Free Start now', 'Pro Start now', ...] — itu paket, bukan model.
_MODEL_COL_RE = re.compile(
    r"^(model|models|model name|name|engine|endpoint|llm|api|resource|"
    # Bentuk lain dari kolom "barang yang dihargai", ditemukan 06/09 di arsip:
    # fireworks-ai memakai "Base Model" dan "GPU Type", replicate "Hardware".
    r"hardware|gpu type|gpu|instance type|instance|sku|tier name|accelerator)$",
    re.I,
)

# Judul kolom pertama yang MENGANDUNG kata "model" juga diterima — misalnya
# "Base model parameter count" (fireworks-ai). Dibatasi panjangnya supaya
# kalimat panjang tidak ikut lolos. Kata "feature"/"fitur" sengaja TIDAK
# pernah diterima: itu penanda tabel perbandingan paket yang diputar
# (firecrawl), bukan tabel harga barang.
_MODEL_COL_LOOSE_RE = re.compile(r"\bmodels?\b", re.I)
_NOT_MODEL_COL_RE = re.compile(r"\b(feature|fitur|capabilit|benefit|include)", re.I)


def _is_model_column(label: str) -> bool:
    if not label or len(label) > 40:
        return False
    if _NOT_MODEL_COL_RE.search(label):
        return False
    return bool(_MODEL_COL_RE.match(label) or _MODEL_COL_LOOSE_RE.search(label))

# Satuan harga biasanya tertulis di caption atau di judul kolom:
# "$ per 1M input tokens", "Price per million tokens", "/1K characters".
_UNIT_RE = re.compile(
    r"per\s+(?:1\s?m|1,000,000|million|1\s?k|1,000|thousand|1\s?b|billion)"
    r"(?:\s+\w+)?\s*(tokens?|characters?|chars?|pixels?|images?|requests?|"
    r"minutes?|seconds?)?"
    r"|/\s*1\s?[mkb]\b"
    r"|per\s+(token|character|image|request|minute|second)s?\b",
    re.I,
)

MAX_TABLES = 20
MAX_MODELS = 400
MAX_MODEL_LEN = 80


@dataclass
class ModelPrice:
    key: str
    model: str
    prices: dict = field(default_factory=dict)   # label kolom -> {raw, amount}
    currency: str | None = None
    unit: str = ""


def _norm_label(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().strip(":").lower()


def _find_header(rows: list[list[str]], look: int = 3) -> int:
    """Indeks baris header, atau -1.

    Header tidak selalu di baris 0: tabel OpenAI memakai dua tingkat, baris 0
    berisi grup kolom ('', 'Short context', 'Long context') dan baris 1 barulah
    ['Model', 'Input', 'Cached input', ...].
    """
    for i, row in enumerate(rows[:look]):
        if row and _is_model_column(_norm_label(row[0])):
            return i
    return -1


def _labels(header: list[str]) -> list[str]:
    """Judul kolom yang unik. Kolom berjudul sama diberi nomor urut.

    Tabel OpenAI punya 'Input' dua kali (konteks pendek dan panjang). Tanpa
    penomoran, yang kedua menimpa yang pertama dan separuh harganya hilang.
    """
    out, count = [], {}
    for cell in header:
        label = _norm_label(cell)
        if not label:
            label = f"kolom {len(out) + 1}"
        count[label] = count.get(label, 0) + 1
        out.append(label if count[label] == 1 else f"{label} ({count[label]})")
    return out


def _unit_for(caption: str, labels: list[str]) -> str:
    for text in [caption, *labels]:
        m = _UNIT_RE.search(text or "")
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip().lower()
    return ""


def _plausible_model(name: str) -> bool:
    n = (name or "").strip()
    if not (1 < len(n) <= MAX_MODEL_LEN):
        return False
    if not re.search(r"[A-Za-z0-9]", n):
        return False
    # Baris subtotal / pemisah bagian, bukan model.
    if _norm_label(n) in {"total", "subtotal", "model", "models", "name", "-", "—"}:
        return False
    return True


def _headerless_labels(rows: list[list[str]]) -> list[str] | None:
    """Tabel tanpa baris judul sama sekali — bentuk together-ai.

    Barisnya langsung data: ['MiniMax M3', '$0.30', '$1.20']. Tidak ada judul
    kolom yang bisa dipakai, jadi kolomnya dinamai menurut posisi. Itu rapuh
    kalau situsnya menambah kolom, maka jumlah kolom IKUT ke dalam kunci
    model: begitu bentuk tabelnya berubah, peristiwanya tercatat sebagai model
    muncul/hilang (perubahan katalog), BUKAN sebagai harga yang bergerak.
    Lebih baik kehilangan satu sinyal daripada mengarang satu.
    """
    from .extract import parse_price

    if len(rows) < 3:
        return None
    ncols = len(rows[0])
    if ncols < 2:
        return None

    baik = 0
    for row in rows:
        if len(row) != ncols:
            return None                       # bentuk tidak konsisten
        nama = (row[0] or "").strip()
        if parse_price(nama)[0] is not None:  # sel pertama berisi harga
            return None                       #  -> ini bukan kolom nama
        if _plausible_model(nama) and any(
                parse_price(c)[0] is not None for c in row[1:]):
            baik += 1
    if baik < max(3, int(len(rows) * 0.8)):
        return None
    return [""] + [f"kolom {i + 1}" for i in range(1, ncols)]


def extract_model_tables(tables: list[dict]) -> list[dict]:
    """Ubah tabel mentah (dari extract.extract_tables) jadi daftar harga model."""
    from .extract import parse_price

    out: list[ModelPrice] = []
    seen: dict[str, dict] = {}

    # Tabel tanpa judul kolom hanya aman kalau nama barisnya UNIK di seluruh
    # halaman. Halaman harga Gemini memuat satu tabel per model, dan semua
    # tabel itu memakai baris yang sama persis: "Input price", "Output price",
    # "Context caching price". Dengan kolom yang dinamai menurut posisi, baris
    # dari model berbeda akan bertabrakan pada kunci yang sama — dan begitu
    # Google menambah satu model, seluruh kunci bergeser dan tercatat sebagai
    # harga yang bergerak. Persis kesalahan tabel-diputar yang sudah kita
    # perbaiki. Kalau nama barisnya berulang antar tabel, jangan dipakai.
    tanpa_judul = [t for t in (tables or [])[:MAX_TABLES]
                   if _find_header(t.get("rows") or []) < 0]
    hitung: dict[str, int] = {}
    for t in tanpa_judul:
        for nama in {(_norm_label(r[0]) if r else "") for r in t.get("rows") or []}:
            if nama:
                hitung[nama] = hitung.get(nama, 0) + 1
    nama_bertabrakan = {n for n, c in hitung.items() if c > 1}

    for table in (tables or [])[:MAX_TABLES]:
        rows = table.get("rows") or []
        h = _find_header(rows)
        suffix = ""
        if h < 0:
            if any(_norm_label(r[0]) in nama_bertabrakan for r in rows if r):
                continue
            labels = _headerless_labels(rows)
            if labels is None:
                continue
            h = -1
            suffix = f"|k{len(labels)}"       # bentuk tabel ikut ke dalam kunci
        else:
            labels = _labels(rows[h])
        unit = _unit_for(table.get("caption", ""), labels)

        data_rows = [r for r in rows[h + 1:] if r and (r[0] or "").strip()]
        parsed: list[tuple[str, dict, str | None]] = []
        for row in data_rows:
            model = re.sub(r"\s+", " ", (row[0] or "")).strip()
            prices: dict[str, dict] = {}
            currency = None
            for i in range(1, min(len(row), len(labels))):
                amount, cur, raw = parse_price(row[i])
                if amount is None:
                    continue
                prices[labels[i]] = {"raw": raw, "amount": amount}
                currency = currency or cur
            if prices and _plausible_model(model):
                parsed.append((model, prices, currency))

        # Tabel yang diputar (model jadi kolom, metrik jadi baris) lolos
        # pemeriksaan header — halaman DeepSeek berjudul kolom pertama "MODEL"
        # tapi barisnya "BASE URL", "CONTEXT LENGTH", "1M INPUT TOKENS". Ciri
        # yang membedakannya bukan kata-katanya, melainkan bentuknya: di tabel
        # harga model yang sungguhan, HAMPIR SEMUA baris punya harga, karena
        # tiap model memang punya harga. Di tabel yang diputar, hanya segelintir
        # baris yang berisi angka.
        if len(parsed) < 2 or len(parsed) * 2 < len(data_rows):
            continue

        for model, prices, currency in parsed:
            base = model.lower() + suffix
            prev = seen.get(base)
            if prev is not None:
                # Sama, atau versi ringkas dari tabel yang sama (tabel yang
                # dicetak ulang dengan kolom lebih sedikit) -> bukan model baru.
                if prices == prev or all(prev.get(k) == v
                                         for k, v in prices.items()):
                    continue
                # Model yang sama dengan angka berbeda (mis. tabel batch):
                # beri kunci sendiri, urut dokumen, supaya tetap stabil.
                n = 2
                while f"{base}#{n}" in seen:
                    n += 1
                base = f"{base}#{n}"

            seen[base] = prices
            out.append(ModelPrice(key=base, model=model, prices=prices,
                                  currency=currency, unit=unit))
            if len(out) >= MAX_MODELS:
                return [asdict(m) for m in out]

    return [asdict(m) for m in out]


# --------------------------------------------------------------------------- #
def _index(models: list[dict] | None) -> dict[str, dict]:
    return {m["key"]: m for m in (models or []) if m.get("key")}


def diff_models(old: list[dict] | None, new: list[dict] | None) -> list[dict]:
    """Peristiwa harga model.

    `old is None` berarti rekaman kemarin dibuat sebelum modul ini ada — belum
    ada dasar pembanding. Diam, jangan laporkan seluruh isi tabel sebagai
    "model baru"; besok baru ada yang bisa dibandingkan.
    """
    if old is None:
        return []

    o, n = _index(old), _index(new)
    events: list[dict] = []

    for key in n.keys() - o.keys():
        events.append({"type": "model_added", "model": n[key]["model"],
                       "prices": n[key].get("prices", {})})
    for key in o.keys() - n.keys():
        events.append({"type": "model_removed", "model": o[key]["model"],
                       "prices": o[key].get("prices", {})})

    for key in sorted(o.keys() & n.keys()):
        before, after = o[key].get("prices", {}), n[key].get("prices", {})
        changes = []
        for field_name in sorted(before.keys() & after.keys()):
            a, b = before[field_name].get("amount"), after[field_name].get("amount")
            if a == b:
                continue
            item = {
                "field": field_name,
                "from": before[field_name],
                "to": after[field_name],
            }
            try:
                if float(a) > 0:
                    item["pct_change"] = round((float(b) - float(a)) / float(a) * 100, 2)
                item["direction"] = "up" if float(b) > float(a) else "down"
            except (TypeError, ValueError):
                pass
            changes.append(item)
        if changes:
            events.append({
                "type": "model_price_changed",
                "model": n[key]["model"],
                "unit": n[key].get("unit", ""),
                "changes": changes,
            })

    return events
