"""Bangun halaman publik dari arsip yang sudah ada.

    python scripts/build_site.py            -> tulis docs/index.html
    python scripts/build_site.py --commit   -> tulis, lalu commit & push docs/

Prinsip yang dipegang berkas ini:

  * TIDAK PERNAH mengambil apa pun dari internet. Sumbernya hanya berkas di
    data/. Halaman ini tidak boleh menambah satu pun permintaan ke situs
    vendor — aturan sekali-sehari tetap berlaku mutlak.
  * TIDAK PERNAH menyentuh data/. Hanya membaca. Kalau berkas ini rusak,
    arsipnya tetap utuh.
  * Hanya menampilkan yang benar-benar tercatat. Tidak ada angka yang
    dikarang, tidak ada perkiraan, tidak ada pembulatan yang menyesatkan.
  * Setiap angka menautkan balik ke halaman harga resminya. Itu janji yang
    kita buat di awal proyek, dan itu juga yang membuat halaman ini pantas
    dipercaya.
  * Keluarannya deterministik: dua kali jalan pada arsip yang sama
    menghasilkan berkas yang sama persis, supaya `git diff` tetap bermakna.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collector import config, storage  # noqa: E402

OUT_DIR = ROOT / "docs"
OUT_FILE = OUT_DIR / "index.html"
# Satu halaman per tool. Alasannya bukan kosmetik: halaman utama tidak akan
# pernah muncul untuk pencarian "cursor pricing history", sedangkan halaman
# yang KHUSUS membahas satu tool bisa. Ini satu-satunya jalur gratis supaya
# arsip ini ditemukan orang — dan sekaligus jawaban atas pertanyaan yang
# benar-benar ditanyakan orang: "harga X naik atau tidak?".
PAGES_DIR = OUT_DIR / "t"
STYLE_FILE = OUT_DIR / "style.css"
SITEMAP_FILE = OUT_DIR / "sitemap.xml"
# Token verifikasi Google Search Console (17/09/2026). Bukan rahasia: memang
# harus terbaca publik di <head> halaman utama. Ditaruh di sini, bukan
# disunting tangan ke docs/index.html, supaya TIDAK hilang setiap kali
# halaman dibangun ulang oleh CI. Search Console memeriksanya berkala; kalau
# tagnya hilang, properti bisa kehilangan verifikasi.
GOOGLE_SITE_VERIFICATION = "H3ICCYaHlGP7r5Q-tXOLi1pNY7CU0k23l29Nj-RCm9s"
RECENT_DAYS = 30
# Batas baris per tabel di halaman utama. Diukur 18/09/2026: halaman utama
# setinggi 25.670 piksel (~32 layar) karena memuat 174 baris perubahan GPU dan
# 165 baris model sekaligus. Sisanya tidak hilang — tiap tool punya halamannya
# sendiri, dan jumlah yang tidak ditampilkan selalu disebutkan.
MAX_BARIS_INDEKS = 20
# Batas baris riwayat di halaman tool. Vast.ai punya 174 pergerakan; kalau
# semuanya dipampang, halamannya setinggi 38.000 piksel di ponsel. Sisanya
# tidak hilang — seluruh catatan tetap terbuka di repositori arsip, dan
# jumlah yang tidak ditampilkan selalu disebutkan.
MAX_BARIS_TOOL = 60
GPU_CATEGORY = "gpu-rental"


# --------------------------------------------------------------------------- #
# pembacaan arsip
# --------------------------------------------------------------------------- #
def load_changes() -> list[dict]:
    if not config.CHANGES_LOG.exists():
        return []
    out = []
    for line in config.CHANGES_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_current(slug: str) -> dict | None:
    path = config.CURRENT_DIR / f"{slug}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def counted(c: dict, corrections: set) -> bool:
    """Boleh tampil / dihitung sebagai pergerakan harga di halaman publik?

    Dua jenis catatan TIDAK boleh:
      * yang sudah dikoreksi (corrections.jsonl) — kita sendiri tahu keliru;
      * `parser_upgrade` — hari ketika PEMBACA angka berubah. Pembanding
        sengaja tetap menyimpan seluruh peristiwanya (supaya tidak ada data
        yang hilang), termasuk `price_changed`, padahal yang bergerak adalah
        pembacanya, bukan harganya. Tanpa penyaring ini, kenaikan
        PARSER_VERSION berikutnya akan menampilkan ulang kesalahan 05/09
        (§10.6) ke publik: mis. Xata "$112 -> $1121 +900%". Ditemukan saat
        audit 10/09/2026, sebelum pernah terjadi.
    """
    if c.get("kind") == "parser_upgrade":
        return False
    return (c.get("date"), c.get("slug"), "price_change") not in corrections


def moved_numbers(entry: dict) -> int:
    return (len([e for e in entry.get("plan_events") or []
                 if e["type"] == "price_changed"])
            + sum(len(e["changes"]) for e in entry.get("model_events") or []
                  if e["type"] == "model_price_changed"))


def last_moves(changes: list[dict], corrections: set) -> dict[tuple, dict]:
    """Pergerakan harga TERAKHIR untuk tiap (slug, nama item).

    Dipakai untuk kolom "perubahan terakhir". Peristiwa yang sudah dikoreksi
    tidak ikut — halaman publik tidak boleh menampilkan angka yang kita
    sendiri sudah tahu keliru.
    """
    out: dict[tuple, dict] = {}
    for c in sorted(changes, key=lambda x: x.get("date", "")):
        if not counted(c, corrections):
            continue
        for e in c.get("plan_events") or []:
            if e["type"] != "price_changed":
                continue
            out[(c["slug"], (e.get("plan") or "").lower())] = {
                "date": c["date"], "from": e["from"].get("raw"),
                "to": e["to"].get("raw"), "pct": e.get("pct_change"),
            }
        for e in c.get("model_events") or []:
            if e["type"] != "model_price_changed":
                continue
            ch = e["changes"][0]
            out[(c["slug"], (e.get("model") or "").lower())] = {
                "date": c["date"], "from": ch["from"].get("raw"),
                "to": ch["to"].get("raw"), "pct": ch.get("pct_change"),
            }
    return out


# --------------------------------------------------------------------------- #
# penyusunan isi
# --------------------------------------------------------------------------- #
def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


_SIMBOL = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "IDR": "Rp "}


def uang(raw, currency=None) -> str:
    """Tampilkan harga apa adanya, tapi jangan biarkan mata uangnya hilang.

    Halaman yang harganya dibaca dari JSON-LD menyimpan `price_raw` berupa
    angka telanjang ("40") dengan `currency` terpisah ("USD") — 173 dari 975
    baris di arsip seperti ini. Di halaman publik itu terbaca "Teams 40",
    yang bagi pembaca tidak berarti apa-apa. Simbol di bawah TIDAK dikarang:
    keduanya diambil dari rekaman yang sama. Kalau mata uangnya tidak
    diketahui, angkanya ditampilkan apa adanya.
    """
    teks = (raw or "").strip()
    if not teks or not currency:
        return teks
    if any(sim in teks for sim in ("$", "€", "£", "¥")) or currency in teks:
        return teks
    simbol = _SIMBOL.get(currency)
    return f"{simbol}{teks}" if simbol else f"{teks} {currency}"


def fmt_pct(pct) -> str:
    if pct in (None, ""):
        return ""
    try:
        v = float(pct)
    except (TypeError, ValueError):
        return ""
    tanda = "+" if v > 0 else ""
    arah = "naik" if v > 0 else "turun"
    return f'<span class="{arah}">{tanda}{v:g}%</span>'


_PERIODE_LABEL = {
    "hour": "per hour", "month": "per month", "year": "per year",
    "day": "per day", "seat": "per seat", "user": "per user",
    "credit": "per credit", "request": "per request",
}


def periode_label(periode) -> str:
    """Satuan waktu apa adanya. Yang tidak tertulis di halaman aslinya
    ditandai jujur, bukan ditebak."""
    if not periode:
        return "not stated"
    return _PERIODE_LABEL.get(periode, str(periode))


def gpu_rows(targets: dict, moves: dict) -> list[dict]:
    """Baris sewa GPU, LENGKAP dengan satuan waktunya.

    Sampai 16/09/2026 tabel ini berjudul "Sewa GPU per jam" padahal 9 baris
    Paperspace bertanda `period: month` — pembaca melihat "$298" di bawah
    judul "per jam" untuk harga yang sebenarnya per bulan. Angkanya benar,
    penyajiannya yang berbohong. Sekarang satuannya ikut ditampilkan dan
    baris bulanan dipisah ke tabelnya sendiri.

    Nama paket disaring dengan `_plausible_plan_name` — penyaring yang SAMA
    dengan yang dipakai pembanding. Jadi halaman publik tidak pernah
    menampilkan sesuatu yang oleh proyek ini sendiri tidak diakui sebagai
    paket (mis. judul bagian "Storage Pricing"). Satu sumber kebenaran, bukan
    daftar kata baru.
    """
    from collector.extract import _plausible_plan_name

    rows = []
    for t in targets.values():
        if t.category != GPU_CATEGORY or not t.enabled:
            continue
        rec = load_current(t.slug)
        if not rec:
            continue
        for p in rec.get("plans") or []:
            if p.get("amount") in (None, ""):
                continue
            if not _plausible_plan_name(p.get("name") or ""):
                continue
            rows.append({
                "vendor": t.name, "url": t.url, "item": p.get("name"),
                "harga": uang(p.get("price_raw"), p.get("currency")),
                "amount": p.get("amount"),
                "periode": p.get("period"),
                "satuan": periode_label(p.get("period")),
                "gerak": moves.get((t.slug, (p.get("name") or "").lower())),
            })
    rows.sort(key=lambda r: (r["vendor"], -(r["amount"] or 0)))
    return rows


_PLACEHOLDER = re.compile(r"^kolom \d+$")


def _harga_ringkas(harga: dict) -> str:
    """Rangkai harga jadi satu kolom yang enak dibaca.

    Tabel tanpa judul kolom memberi label sementara "kolom 2", "kolom 3".
    Itu wajar di dalam arsip, tapi di halaman publik "kolom 2: $0.21" tidak
    memberi tahu pembaca apa pun. Angkanya nyata, hanya namanya yang tidak
    diketahui — jadi tampilkan angkanya saja, tanpa label karangan.
    """
    bagian = []
    for k, v in list(harga.items())[:4]:
        raw = (v.get("raw") or "").strip()
        bagian.append(raw if _PLACEHOLDER.match(k) else f"{k}: {raw}")
    return " · ".join(bagian)


def model_rows(targets: dict, moves: dict, *, api: bool) -> list[dict]:
    """api=True -> hanya kategori ai-api. api=False -> sisanya (non-GPU).

    Dipisah karena tingkat pemakaian Datadog dan ukuran instance Xata bukan
    "model API". Menyatukannya di bawah satu judul membuat halaman ini
    terlihat asal-asalan bagi pembaca yang tahu bedanya.
    """
    rows = []
    for t in targets.values():
        if not t.enabled or t.category == GPU_CATEGORY:
            continue
        if (t.category == "ai-api") != api:
            continue
        rec = load_current(t.slug)
        if not rec:
            continue
        for m in rec.get("models") or []:
            harga = m.get("prices") or {}
            if not harga:
                continue
            rows.append({
                "vendor": t.name, "url": t.url, "item": m.get("model"),
                "harga": _harga_ringkas(harga),
                "satuan": m.get("unit") or "",
                "gerak": moves.get((t.slug, (m.get("model") or "").lower())),
            })
    rows.sort(key=lambda r: (r["vendor"], r["item"] or ""))
    return rows


def _buang_kembar(rows: list[dict]) -> list[dict]:
    """Satu pergerakan harga cukup tampil sekali.

    Halaman harga API kadang terbaca dua kali: sekali sebagai "paket" dan
    sekali sebagai baris tabel model. DeepInfra 07/09 tampil dua kali di
    halaman publik dengan angka yang sama persis, dan ikut terhitung dua kali.
    Baris yang memuat label kolom (mis. "... · $ per 1m input tokens") lebih
    informatif, jadi itu yang dipertahankan.
    """
    terbaik: dict[tuple, dict] = {}
    urutan: list[tuple] = []
    for r in rows:
        dasar = (r.get("item") or "").split(" · ")[0].strip().lower()
        kunci = (r.get("date"), r.get("vendor"), dasar, r.get("dari"), r.get("ke"))
        lama = terbaik.get(kunci)
        if lama is None:
            terbaik[kunci] = r
            urutan.append(kunci)
        elif " · " in (r.get("item") or "") and " · " not in (lama.get("item") or ""):
            terbaik[kunci] = r
    return [terbaik[k] for k in urutan]


def recent_rows(changes: list[dict], corrections: set,
                until: str, gpu_slugs: set | None = None) -> list[dict]:
    batas = (dt.date.fromisoformat(until)
             - dt.timedelta(days=RECENT_DAYS)).isoformat()
    out = []
    for c in changes:
        if c.get("date", "") < batas:
            continue
        if not counted(c, corrections):
            continue
        for e in c.get("plan_events") or []:
            if e["type"] != "price_changed":
                continue
            out.append({"date": c["date"], "vendor": c.get("name"),
                        "url": c.get("url"), "item": e.get("plan"),
                        "dari": uang(e["from"].get("raw"),
                                     e["from"].get("currency")),
                        "ke": uang(e["to"].get("raw"), e["to"].get("currency")),
                        "pct": e.get("pct_change"),
                        "gpu": c.get("slug") in (gpu_slugs or set())})
        for e in c.get("model_events") or []:
            if e["type"] != "model_price_changed":
                continue
            for ch in e["changes"]:
                out.append({"date": c["date"], "vendor": c.get("name"),
                            "url": c.get("url"),
                            "item": f"{e.get('model')} · {ch['field']}",
                            "dari": ch["from"].get("raw"),
                            "ke": ch["to"].get("raw"),
                            "pct": ch.get("pct_change"),
                            "gpu": c.get("slug") in (gpu_slugs or set())})
    out.sort(key=lambda r: (r["date"], r["vendor"] or ""), reverse=True)
    return _buang_kembar(out)


# --------------------------------------------------------------------------- #
# penulisan HTML
# --------------------------------------------------------------------------- #
CSS = """
:root{--bg:#fbfbfa;--fg:#1c1b19;--dim:#6b6862;--line:#e4e1db;--card:#fff;
      --naik:#a33a2a;--turun:#2c6e49;--link:#2a5db0}
@media (prefers-color-scheme:dark){
  :root{--bg:#15140f;--fg:#eceae4;--dim:#9b968c;--line:#2e2c26;--card:#1d1b16;
        --naik:#e8836f;--turun:#7fc8a0;--link:#8fb4f0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:15px/1.6 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:0 16px 64px}
header{padding-block:40px 24px;border-bottom:1px solid var(--line)}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.01em}
h2{font-size:19px;margin:44px 0 4px;letter-spacing:-.01em}
p.sub{color:var(--dim);margin:0}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:20px 0 0;padding:0;list-style:none}
.stats li{background:var(--card);border:1px solid var(--line);border-radius:8px;
          padding:10px 14px;min-width:120px}
.stats b{display:block;font-size:20px;line-height:1.2}
.stats span{color:var(--dim);font-size:12px}
.tw{overflow-x:auto;border:1px solid var(--line);border-radius:8px;
    background:var(--card);margin-top:14px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);
      white-space:nowrap}
th{font-weight:600;color:var(--dim);font-size:12px;text-transform:uppercase;
   letter-spacing:.04em}
tr:last-child td{border-bottom:0}
td.wrap-ok{white-space:normal;min-width:220px}
a{color:var(--link)}
.naik{color:var(--naik);font-weight:600}
.turun{color:var(--turun);font-weight:600}
.dim{color:var(--dim)}
footer{margin-top:56px;padding-top:20px;border-top:1px solid var(--line);
       color:var(--dim);font-size:13px}
footer li{margin-bottom:4px}
@media(max-width:520px){h1{font-size:22px}.stats li{flex:1 1 45%}}
/* Layar sempit: tabel jadi kartu "label: nilai".
   Diukur 18/09/2026 pada 390px — semua tabel memotong kolom harganya, jadi
   pengunjung dari ponsel tidak pernah melihat satu angka pun. Menggeser
   tabel ke samping bukan jawaban: kebanyakan orang tidak tahu bisa digeser. */
@media(max-width:640px){
  .tw{overflow-x:visible;border:0;background:transparent;margin-top:10px}
  table{font-size:15px}
  thead{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
  table,tbody,tr,td{display:block;width:100%}
  tr{background:var(--card);border:1px solid var(--line);border-radius:8px;
     margin-bottom:10px;padding:4px 0}
  td{border:0;white-space:normal;display:flex;gap:14px;
     justify-content:space-between;align-items:baseline;padding:4px 12px}
  td::before{content:attr(data-label);color:var(--dim);font-size:12px;
     text-transform:uppercase;letter-spacing:.04em;flex:0 0 auto}
  td.wrap-ok{min-width:0}
  td>br{display:none}
}
"""


def table(headers: list[str], rows: list,
          kosong: str = "No data in this range.") -> str:
    """Tabel yang tetap terbaca di ponsel.

    `rows` = daftar baris; tiap baris daftar sel (HTML yang sudah di-escape,
    atau pasangan (html, kelas-css)). Label kolom ikut ditanam di tiap sel
    lewat `data-label`, dan CSS di layar sempit mengubah tiap baris jadi
    kartu "label: nilai".

    Kenapa: diukur 18/09/2026 pada layar 390px — SEMUA tabel memotong kolom
    harganya. Halaman "Riwayat harga CodeRabbit" hanya memperlihatkan tanggal
    dan nama item; kolom Dari/Ke/Selisih ada di luar layar, dan pengunjung
    dari Google (mayoritas ponsel) tidak pernah melihat satu angka pun.
    """
    if not rows:
        return f'<p class="dim">{esc(kosong)}</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    badan = []
    for baris in rows:
        sel = []
        for i, isi in enumerate(baris):
            kelas = ""
            if isinstance(isi, tuple):
                isi, kelas = isi
            label = headers[i] if i < len(headers) else ""
            atribut = f' class="{kelas}"' if kelas else ""
            sel.append(f'<td data-label="{esc(label)}"{atribut}>{isi}</td>')
        badan.append("<tr>" + "".join(sel) + "</tr>")
    return (f'<div class="tw"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(badan)}</tbody></table></div>')


def potong(rows: list, batas: int) -> tuple:
    """Kembalikan (baris yang ditampilkan, sisa yang tidak ditampilkan).

    Halaman utama sempat setinggi 25.670 piksel — sekitar 32 layar penuh —
    karena memuat 174 baris perubahan GPU dan 165 baris model sekaligus.
    Tidak ada pembaca yang menggulir sejauh itu. Sekarang tiap tool punya
    halamannya sendiri, jadi halaman utama cukup jadi ringkasan yang
    mengarahkan. Jumlah yang disembunyikan SELALU disebutkan — tidak ada
    yang dihilangkan diam-diam.
    """
    if len(rows) <= batas:
        return rows, 0
    return rows[:batas], len(rows) - batas


def catatan_sisa(sisa: int, total: int) -> str:
    if not sisa:
        return ""
    return (f'<p class="dim">Showing the {total - sisa} most recent of {total}. '
            f'The rest is on each tool’s own page below.</p>')


def build_html(*, tanggal: str, hari: int, halaman: int, angka: int,
               gpu: list[dict], gpu_lain: list[dict], model: list[dict],
               lain: list[dict], terbaru: list[dict], terbaru_gpu: list[dict],
               repo: str, parser_version: int, daftar: str = "") -> str:
    def sumber(url, vendor):
        return f'<a href="{esc(url)}" rel="nofollow noopener">{esc(vendor)}</a>'

    def gerak(g):
        if not g:
            return '<span class="dim">—</span>'
        return (f'{esc(g["from"])} → {esc(g["to"])} {fmt_pct(g["pct"])}'
                f'<br><span class="dim">{esc(g["date"])}</span>')

    def baris_sewa(rows):
        return [[sumber(r["url"], r["vendor"]), esc(r["item"]),
                 esc(r["harga"]), esc(r["satuan"]), gerak(r["gerak"])]
                for r in rows]

    def baris_satuan(rows):
        return [[sumber(r["url"], r["vendor"]), esc(r["item"]),
                 (esc(r["harga"]), "wrap-ok"), esc(r["satuan"]),
                 gerak(r["gerak"])] for r in rows]

    def baris_perubahan(rows):
        return [[esc(r["date"]), sumber(r["url"], r["vendor"]),
                 (esc(r["item"]), "wrap-ok"), esc(r["dari"]), esc(r["ke"]),
                 fmt_pct(r["pct"])] for r in rows]

    gpu_tampil, gpu_sisa = potong(terbaru_gpu, MAX_BARIS_INDEKS)
    sewa_tampil, sewa_sisa = potong(gpu, MAX_BARIS_INDEKS)
    sewa_lain_tampil, sewa_lain_sisa = potong(gpu_lain, MAX_BARIS_INDEKS)

    kol_ubah = ["Date (UTC)", "Source", "Item", "From", "To", "Change"]
    kol_sewa = ["Provider", "Item", "Latest price", "Unit", "Last change"]
    lapor = f"{repo}/issues/new?title=Wrong+number+report"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI &amp; Software Pricing Change Archive</title>
<meta name="description" content="A daily archive of price changes across AI tools, model APIs and GPU rentals. Collected once a day; every number links back to the vendor's official pricing page.">
<link rel="stylesheet" href="style.css">
<link rel="canonical" href="{esc(base_url())}">
{meta_verifikasi()}
</head>
<body>
<div class="wrap">
<header>
  <h1>AI &amp; Software Pricing Change Archive</h1>
  <p class="sub">We check {halaman} official pricing pages once a day and
     record what changed. Every number links back to the vendor’s own page —
     always check there before you decide.</p>
  <ul class="stats">
    <li><b>{halaman}</b><span>pages tracked</span></li>
    <li><b>{hari}</b><span>days archived</span></li>
    <li><b>{angka}</b><span>price moves recorded</span></li>
    <li><b>{esc(tanggal)}</b><span>last check (UTC)</span></li>
  </ul>
</header>

<h2>Software &amp; API price changes — last {RECENT_DAYS} days</h2>
<p class="sub">Only numbers that actually moved on an item present on two
   consecutive days. Plans appearing or disappearing are not counted here.
   GPU rentals are in their own table below: their prices follow the spot
   market and move almost daily, so mixing them in would bury the handful of
   software changes that matter.</p>
{table(kol_ubah, baris_perubahan(terbaru))}

<h2>GPU rental price changes — last {RECENT_DAYS} days</h2>
<p class="sub">Market prices. They move almost every day, and that is
   normal.</p>
{table(kol_ubah, baris_perubahan(gpu_tampil))}
{catatan_sisa(gpu_sisa, len(terbaru_gpu))}

<h2>GPU &amp; infrastructure — hourly rates</h2>
<p class="sub">The last figures we recorded, not an offer. Some providers
   price per card type, others per service (storage, CPU) — both are shown as
   written on the original page.</p>
{table(kol_sewa, baris_sewa(sewa_tampil))}
{catatan_sisa(sewa_sisa, len(gpu))}

<h2>GPU &amp; infrastructure — other units, or unit not stated</h2>
<p class="sub">Rows whose source page bills per month, or does not state a
   unit at all. Kept separate so they are never read as hourly rates. When a
   page states no unit, we say so rather than guess.</p>
{table(kol_sewa, baris_sewa(sewa_lain_tampil))}
{catatan_sisa(sewa_lain_sisa, len(gpu_lain))}

<h2>API model &amp; per-unit prices</h2>
<p class="sub">We track {len(model)} API model prices and {len(lain)} other
   per-unit prices (instance types, usage tiers). They live on each
   provider’s own page below, next to that provider’s change history —
   a single flat table of {len(model) + len(lain)} rows helps nobody.</p>

<h2>All tracked tools</h2>
<p class="sub">Every tool has its own page: the latest prices we recorded and
   its full change history. The number in brackets is how many price moves we
   have recorded so far.</p>
{daftar}

<footer>
  <p><b>How this data is collected</b></p>
  <ul>
    <li>One request per page per day, never more.</li>
    <li><code>robots.txt</code> and each site’s terms are respected; pages
        that disallow it are not fetched.</li>
    <li>Never anything behind a login or a paywall.</li>
    <li>The bot identifies itself and publishes a contact URL.</li>
  </ul>
  <p><b>Accuracy limits.</b> Numbers are read automatically from pricing
     pages, and pricing pages change shape often. Misreadings happen. The
     vendor’s own page is always what counts — not this one. Every raw
     record, including the ones later proven wrong, stays open in the
     <a href="{esc(repo)}" rel="noopener">archive repository</a>.</p>
  <p><b>Found a wrong number?</b> Please tell us — name the tool and the date:
     <a href="{esc(lapor)}" rel="noopener">report it on GitHub Issues</a>.
     Every report is checked against that day’s raw snapshot.</p>
  <p>Product names, trademarks and logos belong to their respective owners.
     This site is not affiliated with, sponsored by, or representing any
     vendor. What is archived here are pricing facts the vendors publish
     themselves on public pages.</p>
  <p>This page is rebuilt automatically whenever the archive grows.
     No ads, no affiliate links, no tracking — your visit is not logged
     anywhere. Dates in UTC · parser version: {parser_version}.</p>
</footer>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
def tanggal_rekaman_terakhir(cadangan: str) -> str:
    """Tanggal pengambilan terakhir, dibaca dari data/runs/.

    Sebelumnya angka ini diambil dari log PERUBAHAN, jadi satu hari tanpa
    perubahan apa pun membuat halaman publik memampang tanggal kemarin
    seolah bot berhenti bekerja. Yang ingin diketahui pembaca adalah kapan
    terakhir kami memeriksa, bukan kapan terakhir ada yang berubah.
    """
    try:
        berkas = sorted(config.RUNS_DIR.glob("*.json"))
        if berkas:
            return berkas[-1].stem
    except Exception:  # noqa: BLE001 — halaman tidak boleh jatuh karena ini
        pass
    return cadangan


def meta_verifikasi() -> str:
    """Tag verifikasi Search Console — hanya di halaman utama, dan hanya
    kalau tokennya memang diisi."""
    if not GOOGLE_SITE_VERIFICATION:
        return ""
    return ('<meta name="google-site-verification" content='
            f'"{esc(GOOGLE_SITE_VERIFICATION)}">')


def base_url() -> str:
    """Alamat situs ini, dihitung dari nama repo — bukan ditulis tangan."""
    pemilik, _, nama = _repo_slug().partition("/")
    return f"https://{pemilik}.github.io/{nama}/"


def riwayat_per_slug(changes: list[dict], corrections: set) -> dict:
    """Seluruh pergerakan harga per tool, terbaru dulu.

    Inilah aset proyek ini yang sebenarnya: bukan harga hari ini — siapa pun
    bisa membuka halaman harga vendor — melainkan kapan harganya berubah,
    dari berapa ke berapa. Sampai 17/09/2026 semua itu terkubur di satu tabel
    besar bercampur 135 tool lain, dan tidak ada satu halaman pun yang bisa
    ditemukan orang yang mencari satu tool tertentu.
    """
    out: dict[str, list[dict]] = {}
    for c in changes:
        if not counted(c, corrections):
            continue
        slug = c.get("slug")
        if not slug:
            continue
        baris = out.setdefault(slug, [])
        for e in c.get("plan_events") or []:
            if e["type"] != "price_changed":
                continue
            baris.append({"date": c["date"], "item": e.get("plan"),
                          "dari": uang(e["from"].get("raw"),
                                       e["from"].get("currency")),
                          "ke": uang(e["to"].get("raw"),
                                     e["to"].get("currency")),
                          "pct": e.get("pct_change")})
        for e in c.get("model_events") or []:
            if e["type"] != "model_price_changed":
                continue
            for ch in e["changes"]:
                baris.append({"date": c["date"],
                              "item": f"{e.get('model')} · {ch['field']}",
                              "dari": ch["from"].get("raw"),
                              "ke": ch["to"].get("raw"),
                              "pct": ch.get("pct_change")})
    for slug, baris in out.items():
        baris.sort(key=lambda r: (r["date"], (r["item"] or "")), reverse=True)
        out[slug] = _buang_kembar(baris)
    return out


def harga_sekarang(rec: dict) -> list[dict]:
    """Baris harga terakhir yang kami rekam untuk satu tool.

    Nama paket disaring dengan penyaring yang SAMA dengan pembanding, supaya
    halaman tool tidak memampang judul bagian sebagai produk.
    """
    from collector.extract import _plausible_plan_name

    rows = []
    for p in rec.get("plans") or []:
        if not (p.get("price_raw") or "").strip():
            continue
        if not _plausible_plan_name(p.get("name") or ""):
            continue
        rows.append({"item": p.get("name"),
                     "harga": uang(p.get("price_raw"), p.get("currency")),
                     "satuan": periode_label(p.get("period"))})
    for m in rec.get("models") or []:
        harga = m.get("prices") or {}
        if not harga:
            continue
        rows.append({"item": m.get("model"), "harga": _harga_ringkas(harga),
                     "satuan": m.get("unit") or periode_label(None)})
    return rows


def tool_page(t, rec: dict, riwayat: list[dict], awal: str, repo: str,
              parser_version: int) -> str:
    """Satu halaman untuk satu tool. TIDAK memuat tanggal hari ini.

    Kalau halaman ini mencantumkan "diperiksa hari ini", 135 berkas berubah
    setiap hari dan riwayat git membengkak tanpa menambah satu pun informasi.
    Semua tanggal di sini berasal dari ARSIP, jadi berkasnya hanya berubah
    kalau datanya memang berubah.

    Teksnya berbahasa Inggris (keputusan pemilik 18/09/2026): orang yang
    mencari "cursor pricing history" atau "h100 rental price" mengetik dalam
    bahasa Inggris, dan seluruh tool yang dipantau produk global. Komentar
    kode tetap bahasa Indonesia.
    """
    sekarang = harga_sekarang(rec)
    baris_harga = [[(esc(r["item"]), "wrap-ok"), (esc(r["harga"]), "wrap-ok"),
                    esc(r["satuan"])] for r in sekarang]
    riwayat_tampil, riwayat_sisa = potong(riwayat, MAX_BARIS_TOOL)
    baris_riwayat = [[esc(r["date"]), (esc(r["item"]), "wrap-ok"),
                      esc(r["dari"]), esc(r["ke"]), fmt_pct(r["pct"])]
                     for r in riwayat_tampil]
    catatan_riwayat = (
        f'<p class="dim">Showing the {len(riwayat_tampil)} most recent of '
        f'{len(riwayat)} price moves. The complete record is in the '
        f'<a href="{esc(repo)}" rel="noopener">archive repository</a>.</p>'
        if riwayat_sisa else "")

    if riwayat:
        ringkas = (f'Last price change we recorded: '
                   f'<b>{esc(riwayat[0]["date"])}</b> — {len(riwayat)} price '
                   f'moves in total since {esc(awal)}.')
        judul_riwayat = f"Price change history ({len(riwayat)})"
    else:
        ringkas = (f'<b>No price change recorded</b> since we started tracking '
                   f'on {esc(awal)}. The pricing page is still checked every '
                   f'day.')
        judul_riwayat = "Price change history"

    kanonik = f"{base_url()}t/{t.slug}.html"
    lapor = f"{repo}/issues/new?title=Wrong+number+report+({t.slug})"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(t.name)} pricing history — daily archive</title>
<meta name="description" content="Price change history for {esc(t.name)}: when it changed, from what to what. Recorded automatically once a day since {esc(awal)}; every number links to the official pricing page.">
<link rel="canonical" href="{esc(kanonik)}">
<link rel="stylesheet" href="../style.css">
</head>
<body>
<div class="wrap">
<header>
  <p class="sub"><a href="../">← All tracked tools</a></p>
  <h1>{esc(t.name)} pricing history</h1>
  <p class="sub">{ringkas}</p>
  <p class="sub">Official source:
     <a href="{esc(t.url)}" rel="nofollow noopener">{esc(t.url)}</a> —
     that page is what counts, not this one.</p>
</header>

<h2>{judul_riwayat}</h2>
{table(["Date (UTC)", "Item", "From", "To", "Change"], baris_riwayat,
       kosong="No price change recorded for this tool yet.")}
{catatan_riwayat}

<h2>Latest prices we recorded</h2>
<p class="sub">Read automatically from the vendor’s official pricing page.
   Units are shown exactly as stated there; when the page states no unit, we
   say so rather than guess.</p>
{table(["Item", "Price", "Unit"], baris_harga)}

<footer>
  <p>Fetched at most once a day, honouring <code>robots.txt</code>, never
     behind a login or a paywall. Numbers are read automatically and can be
     wrong — <a href="{esc(lapor)}" rel="noopener">tell us if you spot one</a>.
     Every raw record stays open in the
     <a href="{esc(repo)}" rel="noopener">archive repository</a>.</p>
  <p>Product names and trademarks belong to their respective owners. This
     site is not affiliated with, sponsored by, or representing any vendor.
     No ads, no affiliate links, no tracking. Dates in UTC · parser version:
     {parser_version}.</p>
</footer>
</div>
</body>
</html>
"""


def sitemap(halaman: list) -> str:
    """sitemap.xml — satu-satunya cara mesin pencari tahu halaman ini ada.

    `lastmod` diambil dari ARSIP, bukan dari jam dinding, supaya berkas ini
    pun tidak berubah tanpa alasan.
    """
    baris = "".join(
        f"  <url><loc>{esc(loc)}</loc><lastmod>{esc(tgl)}</lastmod></url>\n"
        for loc, tgl in halaman)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f'{baris}</urlset>\n')


def build_pages(targets: dict, changes: list[dict], corrections: set,
                awal: str, akhir: str) -> dict:
    """Kembalikan {jalur relatif -> isi} untuk seluruh halaman per tool +
    sitemap. Tidak menulis apa pun — supaya bisa diuji tanpa menyentuh disk."""
    riwayat = riwayat_per_slug(changes, corrections)
    repo = f"https://github.com/{_repo_slug()}"
    keluaran: dict[str, str] = {}
    url_sitemap = [(base_url(), akhir)]
    for slug in sorted(targets):
        t = targets[slug]
        if not t.enabled:
            continue
        rec = load_current(slug)
        if not rec:
            continue
        r = riwayat.get(slug, [])
        keluaran[f"t/{slug}.html"] = tool_page(
            t, rec, r, awal, repo, config.PARSER_VERSION)
        url_sitemap.append((f"{base_url()}t/{slug}.html",
                            r[0]["date"] if r else awal))
    keluaran["sitemap.xml"] = sitemap(url_sitemap)
    return keluaran


# Label kategori untuk pembaca. Ini MURNI label tampilan — nilai aslinya di
# targets.yaml tidak diubah (slug dan kategori adalah kunci arsip). Kategori
# yang belum punya label tampil apa adanya, bukan disembunyikan.
_LABEL_KATEGORI = {
    "ai-api": "AI model APIs",
    "ai-assistant": "AI assistants",
    "ai-coding": "AI coding tools",
    "ai-media": "AI image, audio & video",
    "automation": "Automation & workflows",
    "database": "Databases",
    "dev-infra": "Developer infrastructure",
    "gpu-rental": "GPU rental",
    "observability": "Observability & security",
    "productivity": "Productivity & collaboration",
}


def label_kategori(kategori: str) -> str:
    return _LABEL_KATEGORI.get(kategori, kategori)


def daftar_tool(targets: dict, riwayat: dict) -> str:
    """Daftar seluruh tool di halaman utama — sekaligus jalan masuk mesin
    pencari ke 135 halaman tool (tanpa tautan internal, halaman itu tidak
    akan pernah ditemukan)."""
    per_kategori: dict[str, list] = {}
    for slug in sorted(targets):
        t = targets[slug]
        if not t.enabled:
            continue
        per_kategori.setdefault(t.category, []).append(t)
    bagian = []
    for kategori in sorted(per_kategori):
        tautan = " · ".join(
            f'<a href="t/{esc(t.slug)}.html">{esc(t.name)}</a>'
            + (f' <span class="dim">({len(riwayat[t.slug])})</span>'
               if riwayat.get(t.slug) else "")
            for t in sorted(per_kategori[kategori], key=lambda x: x.name.lower()))
        bagian.append(f'<p class="sub"><b>{esc(label_kategori(kategori))}</b>'
                      f'<br>{tautan}</p>')
    return "\n".join(bagian)



def build() -> dict:
    """Kembalikan {jalur relatif di docs/ -> isi}. Tidak menulis apa pun."""
    targets = {t.slug: t for t in config.load_targets()}
    changes = load_changes()
    corrections = storage.load_corrections()

    tanggal_semua = sorted({c["date"] for c in changes if c.get("date")})
    if not tanggal_semua:
        raise SystemExit("arsip masih kosong — tidak ada yang bisa dibangun")
    awal, akhir = tanggal_semua[0], tanggal_semua[-1]
    hari = (dt.date.fromisoformat(akhir) - dt.date.fromisoformat(awal)).days + 1

    moves = last_moves(changes, corrections)
    angka = sum(moved_numbers(c) for c in changes if counted(c, corrections))

    # Pemisahan software vs GPU memakai definisi yang SAMA dengan
    # gate_status.py — kategori `gpu-rental` di targets.yaml. Satu definisi
    # untuk semua penghitung, supaya halaman publik dan angka gerbang tidak
    # pernah bercerita berbeda.
    gpu_slugs = {t.slug for t in targets.values()
                 if t.category == GPU_CATEGORY}

    sewa = gpu_rows(targets, moves)
    terbaru_semua = recent_rows(changes, corrections, akhir, gpu_slugs)
    riwayat = riwayat_per_slug(changes, corrections)

    index = build_html(
        tanggal=tanggal_rekaman_terakhir(akhir), hari=hari,
        halaman=len([t for t in targets.values() if t.enabled]),
        angka=angka,
        gpu=[r for r in sewa if r["periode"] == "hour"],
        gpu_lain=[r for r in sewa if r["periode"] != "hour"],
        model=model_rows(targets, moves, api=True),
        lain=model_rows(targets, moves, api=False),
        terbaru=[r for r in terbaru_semua if not r.get("gpu")],
        terbaru_gpu=[r for r in terbaru_semua if r.get("gpu")],
        repo=f"https://github.com/{_repo_slug()}",
        parser_version=config.PARSER_VERSION,
        daftar=daftar_tool(targets, riwayat),
    )

    berkas = {"index.html": index, "style.css": CSS.strip() + "\n"}
    berkas.update(build_pages(targets, changes, corrections, awal, akhir))
    return berkas


def _repo_slug() -> str:
    import os

    return os.environ.get("GITHUB_REPOSITORY", "szeto214/ai-pricing-tracker")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def commit_and_push(tanggal: str) -> int:
    """Commit docs/ SAJA. Arsip di data/ tidak pernah disentuh dari sini."""
    _git("config", "user.name", "pricing-bot")
    _git("config", "user.email",
         "41898282+github-actions[bot]@users.noreply.github.com")
    _git("add", "-A", "docs/")
    if _git("diff", "--cached", "--quiet", "--", "docs/").returncode == 0:
        print("halaman publik tidak berubah — tidak ada commit")
        return 0
    # `-- docs/`: commit HANYA docs/, walau ada berkas lain yang kebetulan
    # sudah di-stage oleh langkah sebelumnya. Commit berlabel "site:" tidak
    # boleh diam-diam membawa isi arsip.
    c = _git("commit", "-m", f"site: perbarui halaman publik ({tanggal})",
             "--", "docs/")
    print(c.stdout.strip()[:300])
    if c.returncode != 0:
        return 1
    ref = (_git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main")
    for attempt in range(1, 4):
        pull = _git("pull", "--rebase", "--autostash", "origin", ref)
        if pull.returncode != 0:
            # Tanpa pemeriksaan ini, rebase yang tersangkut membuat `push`
            # menjawab "Everything up-to-date" dan skrip melaporkan SUKSES
            # padahal halaman tidak terbit (ditemukan saat audit 10/09/2026).
            # Batalkan rebase (kalau ada) dan coba lagi; gangguan jaringan
            # sesaat tetap punya kesempatan pulih. Kalau semua percobaan
            # gagal, laporkan gagal dengan jujur — halaman bisa dibangun ulang
            # kapan saja, arsipnya sudah aman di langkah sebelumnya.
            print(f"pull --rebase gagal (percobaan {attempt}):\n"
                  f"{pull.stdout[-500:]}")
            _git("rebase", "--abort")
            continue
        p = _git("push", "origin", f"HEAD:{ref}")
        if p.returncode == 0:
            print(p.stdout.strip()[-300:])
            return 0
        print(f"push halaman gagal (percobaan {attempt}):\n{p.stdout[-500:]}")
    return 1


def buang_halaman_usang(berkas: dict) -> int:
    """Target yang dinonaktifkan tidak boleh meninggalkan halaman yatim yang
    terus tayang dengan angka basi. Yang dihapus HANYA berkas `.html` di
    docs/t/ yang memang kita hasilkan sendiri — tidak pernah menyentuh yang
    lain."""
    dihapus = 0
    if not PAGES_DIR.exists():
        return 0
    for lama in sorted(PAGES_DIR.glob("*.html")):
        if f"t/{lama.name}" not in berkas:
            lama.unlink()
            dihapus += 1
    return dihapus


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="commit & push docs/ setelah dibangun")
    args = ap.parse_args()

    if args.commit:
        # Halaman publik hanya boleh memuat angka yang SUDAH ada di arsip git.
        # Kalau data/ masih punya perubahan yang belum di-commit — misalnya
        # kolektor jatuh di tengah jalan setelah menulis sebagian
        # data/current/, sehingga langkah "Commit snapshot" dilewati —
        # menerbitkan halaman berarti memamerkan angka yang tidak pernah
        # masuk arsip. Ditemukan saat audit 10/09/2026 (dibuktikan pada
        # salinan repo). Lebih baik halaman tertinggal sehari daripada
        # menampilkan angka tanpa jejak.
        kotor = _git("status", "--porcelain", "--", "data/").stdout.strip()
        if kotor:
            print("! data/ punya perubahan yang belum ter-commit — halaman "
                  "TIDAK diterbitkan hari ini:\n" + kotor[:500])
            return 1

    berkas = build()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    # .nojekyll: tanpa ini GitHub Pages menjalankan Jekyll dan mengabaikan
    # berkas/direktori berawalan garis bawah. Kita tidak memakainya, tapi
    # mematikannya membuat perilakunya bisa ditebak.
    (OUT_DIR / ".nojekyll").write_text("", encoding="utf-8")

    total = 0
    for rel, isi in berkas.items():
        jalur = OUT_DIR / rel
        jalur.parent.mkdir(parents=True, exist_ok=True)
        jalur.write_text(isi, encoding="utf-8")
        total += len(isi)

    dihapus = buang_halaman_usang(berkas)

    print(f"ditulis: {len(berkas)} berkas di docs/ ({total:,} byte)"
          + (f", {dihapus} halaman usang dihapus" if dihapus else ""))

    if args.commit:
        return commit_and_push(dt.date.today().isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
