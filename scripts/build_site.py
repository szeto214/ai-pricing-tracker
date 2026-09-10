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
RECENT_DAYS = 30
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
        if (c.get("date"), c.get("slug"), "price_change") in corrections:
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


def gpu_rows(targets: dict, moves: dict) -> list[dict]:
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
            rows.append({
                "vendor": t.name, "url": t.url, "item": p.get("name"),
                "harga": p.get("price_raw"),
                "amount": p.get("amount"),
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


def recent_rows(changes: list[dict], corrections: set,
                until: str) -> list[dict]:
    batas = (dt.date.fromisoformat(until)
             - dt.timedelta(days=RECENT_DAYS)).isoformat()
    out = []
    for c in changes:
        if c.get("date", "") < batas:
            continue
        if (c.get("date"), c.get("slug"), "price_change") in corrections:
            continue
        for e in c.get("plan_events") or []:
            if e["type"] != "price_changed":
                continue
            out.append({"date": c["date"], "vendor": c.get("name"),
                        "url": c.get("url"), "item": e.get("plan"),
                        "dari": e["from"].get("raw"), "ke": e["to"].get("raw"),
                        "pct": e.get("pct_change")})
        for e in c.get("model_events") or []:
            if e["type"] != "model_price_changed":
                continue
            for ch in e["changes"]:
                out.append({"date": c["date"], "vendor": c.get("name"),
                            "url": c.get("url"),
                            "item": f"{e.get('model')} · {ch['field']}",
                            "dari": ch["from"].get("raw"),
                            "ke": ch["to"].get("raw"),
                            "pct": ch.get("pct_change")})
    out.sort(key=lambda r: (r["date"], r["vendor"] or ""), reverse=True)
    return out


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
"""


def table(headers: list[str], baris: list[str]) -> str:
    if not baris:
        return '<p class="dim">Belum ada data pada rentang ini.</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    return (f'<div class="tw"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(baris)}</tbody></table></div>')


def build_html(*, tanggal: str, hari: int, halaman: int, angka: int,
               gpu: list[dict], model: list[dict], lain: list[dict],
               terbaru: list[dict], repo: str) -> str:
    def sumber(url, vendor):
        return f'<a href="{esc(url)}" rel="nofollow noopener">{esc(vendor)}</a>'

    def gerak(g):
        if not g:
            return '<span class="dim">—</span>'
        return (f'{esc(g["from"])} → {esc(g["to"])} {fmt_pct(g["pct"])}'
                f'<br><span class="dim">{esc(g["date"])}</span>')

    baris_gpu = [
        f'<tr><td>{sumber(r["url"], r["vendor"])}</td>'
        f'<td>{esc(r["item"])}</td><td>{esc(r["harga"])}</td>'
        f'<td>{gerak(r["gerak"])}</td></tr>' for r in gpu]

    def baris_satuan(rows):
        return [
            f'<tr><td>{sumber(r["url"], r["vendor"])}</td>'
            f'<td>{esc(r["item"])}</td><td class="wrap-ok">{esc(r["harga"])}</td>'
            f'<td>{esc(r["satuan"])}</td><td>{gerak(r["gerak"])}</td></tr>'
            for r in rows]

    baris_model = baris_satuan(model)
    baris_lain = baris_satuan(lain)

    baris_baru = [
        f'<tr><td>{esc(r["date"])}</td><td>{sumber(r["url"], r["vendor"])}</td>'
        f'<td class="wrap-ok">{esc(r["item"])}</td><td>{esc(r["dari"])}</td>'
        f'<td>{esc(r["ke"])}</td><td>{fmt_pct(r["pct"])}</td></tr>'
        for r in terbaru]

    return f"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arsip Perubahan Harga Tool AI &amp; Software</title>
<meta name="description" content="Arsip harian perubahan harga tool AI, API model, dan sewa GPU. Dikumpulkan otomatis sekali sehari, setiap angka tertaut ke halaman harga resminya.">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>Arsip Perubahan Harga Tool AI &amp; Software</h1>
  <p class="sub">Direkam otomatis sekali sehari. Setiap angka tertaut ke
     halaman harga resminya — selalu periksa di sumbernya sebelum mengambil
     keputusan.</p>
  <ul class="stats">
    <li><b>{halaman}</b><span>halaman dipantau</span></li>
    <li><b>{hari}</b><span>hari arsip</span></li>
    <li><b>{angka}</b><span>angka harga bergerak</span></li>
    <li><b>{esc(tanggal)}</b><span>rekaman terakhir</span></li>
  </ul>
</header>

<h2>Perubahan harga {RECENT_DAYS} hari terakhir</h2>
<p class="sub">Hanya angka yang benar-benar bergerak pada item yang ada di dua
   hari berturut-turut. Penambahan atau penghapusan paket tidak dihitung di
   sini.</p>
{table(["Tanggal", "Sumber", "Item", "Dari", "Ke", "Selisih"], baris_baru)}

<h2>Sewa GPU per jam</h2>
<p class="sub">Harga pasar sewa GPU bergerak hampir setiap hari. Angka di
   bawah adalah rekaman terakhir kami, bukan penawaran.</p>
{table(["Penyedia", "Kartu", "Harga terakhir", "Perubahan terakhir"], baris_gpu)}

<h2>Harga model API</h2>
<p class="sub">Dibaca dari tabel harga resmi tiap penyedia. Kolomnya mengikuti
   penamaan di halaman aslinya.</p>
{table(["Penyedia", "Model", "Harga", "Satuan", "Perubahan terakhir"], baris_model)}

<h2>Harga per satuan lainnya</h2>
<p class="sub">Tipe instance, tingkat pemakaian, dan satuan lain yang dihargai
   per baris tabel — di luar model API dan sewa GPU.</p>
{table(["Penyedia", "Item", "Harga", "Satuan", "Perubahan terakhir"], baris_lain)}

<footer>
  <p><b>Cara data ini dikumpulkan</b></p>
  <ul>
    <li>Satu permintaan per halaman per hari, tidak lebih.</li>
    <li><code>robots.txt</code> dan ketentuan layanan tiap situs dihormati;
        yang melarang tidak diambil.</li>
    <li>Tidak pernah mengambil di balik login atau paywall.</li>
    <li>Bot mengidentifikasi dirinya dan mencantumkan URL kontak.</li>
  </ul>
  <p><b>Batas ketelitian.</b> Angka dibaca otomatis dari halaman harga, dan
     halaman harga sering berubah bentuk. Kekeliruan pembacaan mungkin
     terjadi. Halaman resmi vendor selalu menjadi acuan — bukan halaman ini.
     Semua rekaman mentah, termasuk yang kemudian terbukti keliru, tersimpan
     terbuka di <a href="{esc(repo)}" rel="noopener">repositori arsip</a>.</p>
  <p>Halaman ini dibangun ulang otomatis setiap kali arsip bertambah.
     Tidak ada iklan, tidak ada tautan afiliasi, tidak ada pelacakan.</p>
</footer>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
def build() -> str:
    targets = {t.slug: t for t in config.load_targets()}
    changes = load_changes()
    corrections = storage.load_corrections()

    tanggal_semua = sorted({c["date"] for c in changes if c.get("date")})
    if not tanggal_semua:
        raise SystemExit("arsip masih kosong — tidak ada yang bisa dibangun")
    awal, akhir = tanggal_semua[0], tanggal_semua[-1]
    hari = (dt.date.fromisoformat(akhir) - dt.date.fromisoformat(awal)).days + 1

    moves = last_moves(changes, corrections)
    angka = sum(moved_numbers(c) for c in changes
                if (c.get("date"), c.get("slug"), "price_change")
                not in corrections)

    return build_html(
        tanggal=akhir, hari=hari,
        halaman=len([t for t in targets.values() if t.enabled]),
        angka=angka,
        gpu=gpu_rows(targets, moves),
        model=model_rows(targets, moves, api=True),
        lain=model_rows(targets, moves, api=False),
        terbaru=recent_rows(changes, corrections, akhir),
        repo=f"https://github.com/{_repo_slug()}",
    )


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
    if _git("diff", "--cached", "--quiet").returncode == 0:
        print("halaman publik tidak berubah — tidak ada commit")
        return 0
    c = _git("commit", "-m", f"site: perbarui halaman publik ({tanggal})")
    print(c.stdout.strip()[:300])
    if c.returncode != 0:
        return 1
    ref = (_git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main")
    for attempt in range(1, 4):
        _git("pull", "--rebase", "--autostash", "origin", ref)
        p = _git("push", "origin", f"HEAD:{ref}")
        if p.returncode == 0:
            print(p.stdout.strip()[-300:])
            return 0
        print(f"push halaman gagal (percobaan {attempt}):\n{p.stdout[-500:]}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="commit & push docs/ setelah dibangun")
    args = ap.parse_args()

    halaman = build()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # .nojekyll: tanpa ini GitHub Pages menjalankan Jekyll dan mengabaikan
    # berkas/direktori berawalan garis bawah. Kita tidak memakainya, tapi
    # mematikannya membuat perilakunya bisa ditebak.
    (OUT_DIR / ".nojekyll").write_text("", encoding="utf-8")
    OUT_FILE.write_text(halaman, encoding="utf-8")
    print(f"ditulis: {OUT_FILE.relative_to(ROOT)}  ({len(halaman):,} byte)")

    if args.commit:
        return commit_and_push(dt.date.today().isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
