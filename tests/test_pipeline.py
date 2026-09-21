"""Uji pipeline tanpa menyentuh internet.

    python -m tests.test_pipeline

Yang diuji:
  1. Stabilitas hash  — nonce/uuid/timestamp yang berubah tiap muat TIDAK boleh
     menghasilkan perubahan. Ini syarat hidup-mati arsip.
  2. Ekstraksi        — kartu paket, JSON-LD, dan tabel harga API.
  3. Diff             — kenaikan harga, paket baru, paket hilang, fitur berubah.
  4. robots.txt       — halaman yang dilarang benar-benar dilewati.
  5. End-to-end       — kolektor jalan dua kali terhadap server lokal:
                        run 1 = first_seen, run 2 = tidak ada perubahan,
                        run 3 setelah harga diubah = price_change tercatat.
"""

from __future__ import annotations

import datetime as dt
import http.server
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import uuid
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="apt-test-"))
os.environ["APT_DATA_DIR"] = str(_TMP / "data")
os.environ["APT_MIN_INTERVAL"] = "0"

from collector import config, extract, normalize, storage  # noqa: E402
from collector.diff import compare, diff_plans  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  GAGAL {label} {detail}")
        FAILURES.append(f"{label} {detail}")


def render(name: str, **subs: str) -> str:
    html = (TESTS / "fixtures" / name).read_text(encoding="utf-8")
    defaults = {
        "__NONCE__": uuid.uuid4().hex,
        "__BUILD__": uuid.uuid4().hex[:12],
        "__UUID__": str(uuid.uuid4()),
        "__TS__": "2026-08-27T09:14:03Z",
    }
    defaults.update(subs)
    for k, v in defaults.items():
        html = html.replace(k, v)
    return html


# --------------------------------------------------------------------------- #
def test_hash_stability() -> None:
    print("\n1. stabilitas hash terhadap noise")
    for fixture in ("cards.html", "jsonld.html", "apitable.html"):
        a = normalize.process(render(fixture))
        b = normalize.process(render(fixture))
        check(f"{fixture}: hash sama meski nonce/uuid berbeda",
              a["content_hash"] == b["content_hash"],
              f"\n       {a['content_hash'][:16]} vs {b['content_hash'][:16]}")
        check(f"{fixture}: hash mentah memang berbeda (noise nyata ada)",
              a["raw_hash"] != b["raw_hash"])

    changed = normalize.process(render("cards.html").replace("$20", "$25"))
    base = normalize.process(render("cards.html"))
    check("perubahan harga sungguhan mengubah hash",
          changed["content_hash"] != base["content_hash"])

    text = base["text"]
    check("banner cookie dibuang dari teks", "Accept all cookies" not in text)
    check("isi utama tetap ada", "Unlimited completions" in text)
    check("teks tidak mengandung kode JS", "window.__BUILD__" not in text)


def test_nested_noise() -> None:
    """Regresi: wadah bising bersarang pernah menjatuhkan 7 target sekaligus.

    Saat sebuah induk di-decompose, bs4 ikut menghancurkan seluruh
    keturunannya dan `tag.attrs` jadi None. Kode lama masih menyentuh tag
    mati itu -> AttributeError -> target gagal total, padahal halamannya
    baik-baik saja.
    """
    print("\n1b. wadah bising bersarang (regresi crash)")
    html = render("nested-noise.html")
    try:
        proc = normalize.process(html)
        crashed = None
    except Exception as exc:  # noqa: BLE001
        proc, crashed = None, f"{type(exc).__name__}: {exc}"
    check("normalisasi tidak crash", crashed is None, f"-> {crashed}")
    if proc is None:
        return

    check("banner consent bersarang terbuang",
          "We value your privacy" not in proc["text"])
    check("widget chat terbuang", "chat" not in proc["text"].lower())
    check("isi harga selamat",
          "Unlimited projects" in proc["text"] and "$49" in proc["text"])

    res = extract.extract("noisy", proc["soup"], html)
    names = sorted(p["name"] for p in res["plans"])
    check("2 paket tetap terekstrak", names == ["Growth", "Starter"], f"-> {names}")

    a = normalize.process(render("nested-noise.html"))
    check("hash tetap stabil", a["content_hash"] == proc["content_hash"])


def test_site_chrome_noise() -> None:
    """Regresi dari snapshot kedua (28/08/2026).

    15 dari 65 halaman tercatat "berubah" padahal harganya diam. Yang bergerak:
    penghitung bintang (firecrawl 173.1K -> 173.4K) dan label menu footer
    (mistral "Legal" -> "Company"). Kalau dibiarkan, firecrawl akan melapor
    berubah SETIAP HARI selamanya dan arsipnya jadi tidak bisa dipercaya.
    """
    print("\n2b. perabot situs & penghitung (regresi derau harian)")

    a = normalize.process(render("chrome-noise.html",
                                __STARS__="173.1K", __FOOTER_LABEL__="Legal"))
    b = normalize.process(render("chrome-noise.html",
                                __STARS__="173.4K", __FOOTER_LABEL__="Company"))

    check("penghitung berubah + label footer berubah -> TIDAK dianggap berubah",
          a["content_hash"] == b["content_hash"],
          f"\n       {a['content_hash'][:16]} vs {b['content_hash'][:16]}")
    check("penghitung tidak tersisa di teks", "173.1K" not in a["text"])
    check("menu navigasi terbuang", "Docs" not in a["text"])
    check("label footer terbuang", "Careers" not in a["text"])

    # Yang penting: batas kuota BUKAN penghitung, dan harus tetap terbaca.
    check("batas kuota tetap utuh", "10K requests per month" in a["text"],
          f"-> {a['text']!r}")
    check("isi harga tetap utuh",
          "$16" in a["text"] and "500 credits per month" in a["text"])

    # Perubahan kuota sungguhan tetap harus terdeteksi.
    c = normalize.process(
        render("chrome-noise.html", __STARS__="173.1K", __FOOTER_LABEL__="Legal")
        .replace("10K requests per month", "5K requests per month"))
    check("penurunan kuota 10K -> 5K tetap terdeteksi",
          c["content_hash"] != a["content_hash"])

    res = extract.extract("noise", a["soup"], render("chrome-noise.html"))
    names = sorted(p["name"] for p in res["plans"])
    check("2 paket tetap terekstrak", names == ["Free", "Standard"], f"-> {names}")


def test_extraction() -> None:
    print("\n2. ekstraksi")
    html = render("cards.html")
    proc = normalize.process(html)
    res = extract.extract("acme", proc["soup"], html)
    names = [p["name"] for p in res["plans"]]
    check("kartu: 3 paket terdeteksi", len(res["plans"]) == 3, f"-> {names}")
    check("kartu: nama paket benar",
          {"Hobby", "Pro", "Business"} <= set(names), f"-> {names}")
    by_name = {p["name"]: p for p in res["plans"]}
    if "Pro" in by_name:
        check("kartu: harga Pro = 20 USD/month",
              by_name["Pro"]["amount"] == 20.0
              and by_name["Pro"]["currency"] == "USD"
              and by_name["Pro"]["period"] == "month",
              f"-> {by_name['Pro']}")
        check("kartu: fitur Pro terambil",
              "Unlimited completions" in by_name["Pro"]["features"])
    if "Hobby" in by_name:
        check("kartu: paket gratis terdeteksi sebagai 0",
              by_name["Hobby"]["amount"] == 0.0, f"-> {by_name['Hobby']}")

    html = render("jsonld.html")
    proc = normalize.process(html)
    res = extract.extract("bolt", proc["soup"], html)
    check("jsonld: dipakai sebagai sumber", res["extractor"] == "jsonld",
          f"-> {res['extractor']}")
    amounts = sorted(p["amount"] for p in res["plans"])
    check("jsonld: 3 harga benar", amounts == [0.0, 29.0, 199.0], f"-> {amounts}")

    html = render("apitable.html")
    proc = normalize.process(html)
    res = extract.extract("nimbus", proc["soup"], html)
    check("tabel API: 1 tabel terambil", len(res["tables"]) == 1,
          f"-> {len(res['tables'])}")
    if res["tables"]:
        rows = res["tables"][0]["rows"]
        check("tabel API: 4 baris (header + 3 model)", len(rows) == 4, f"-> {rows}")
        check("tabel API: caption terbaca",
              res["tables"][0]["caption"] == "Text models")


def test_secret_redaction() -> None:
    """Regresi: satu contoh kredensial di halaman harga menolak SELURUH commit.

    Push protection GitHub menolak push #1 gara-gara contoh password PlanetScale
    di halaman harga mereka. 64 halaman lain ikut gagal tersimpan.
    """
    print("\n3b. penyamaran token berbentuk kredensial")

    # JANGAN menyatukan potongan-potongan ini menjadi satu literal.
    # Semuanya token palsu, tapi push protection GitHub memindai isi berkas
    # dan menolak push kalau polanya utuh — versi pertama berkas ini benar-benar
    # ditolak karena kunci Stripe dan Slack palsu di sini. Dipecah supaya
    # berkasnya sendiri tidak pernah cocok dengan pola apa pun.
    samples = [
        ("PlanetScale", ["mysql://user:pscale_", "pw_aBcD1234EfGh5678IjKl9012@h"]),
        ("OpenAI", ["Authorization: Bearer sk", "-proj1234567890ABCDEFGHIJKLMNOP"]),
        ("Anthropic", ["x-api-key: sk-ant", "-api03-AbCdEfGh1234567890IjKlMnOp"]),
        ("GitHub PAT", ["token ghp", "_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"]),
        ("AWS", ["AKIA", "IOSFODNN7EXAMPLE"]),
        ("Stripe", ["sk_", "live_51ABCDEFGHIJKLMNOPQRSTUVWX"]),
        ("Slack", ["xoxb", "-123456789012-abcdefghijklmnop"]),
        ("JWT", ["eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0", "NTY3ODkw.dBjftJeZ4CVPmB92K"]),
        ("generik", ["acme_", "token_QWERTYUIOPASDFGHJKL123"]),
    ]
    for label, parts in samples:
        raw = "".join(parts)
        html = f"<html><body><p>{raw}</p><p>Pro $20/month</p></body></html>"
        text = normalize.to_text(normalize.clean_html(html)[0])
        leaked = [tok for tok in raw.replace(":", " ").replace("/", " ").split()
                  if len(tok) > 24 and tok in text]
        check(f"{label} disamarkan", not leaked, f"-> bocor: {leaked}")

    # Harga di halaman yang sama tidak boleh ikut hilang.
    token = "pscale_" + "pw_aBcD1234EfGh5678IjKl9012"
    html = (f"<html><body><p>{token}</p>"
            "<p>Scaler Pro $39/month</p></body></html>")
    text = normalize.to_text(normalize.clean_html(html)[0])
    check("harga di halaman yang sama tetap utuh", "$39" in text, f"-> {text!r}")


def test_plan_name_sanity() -> None:
    """Regresi dari snapshot ketiga (29/08/2026).

    Ringkasan melaporkan "Perubahan harga: 4", padahal keempatnya palsu:
    judul bagian ("Everything in Pro and:"), nama model ("Kimi K2.7 Code"),
    dan bahkan angka harga ("$4.00") terbaca sebagai NAMA PAKET, lalu
    penambahan/penghapusannya dihitung sebagai perubahan harga. Metrik itu
    yang menentukan gerbang bulan ke-3 — kalau menggelembung, gerbangnya
    kehilangan arti.
    """
    print("\n3c. saringan nama paket")
    from collector.extract import _plausible_plan_name as ok

    for bad in ["Everything in Pro and:", "Everything in Free and", "$4.00",
                "1.234", "Includes everything in Team:", "", "x",
                "Get started free", "Contact sales",
                # Kasus nyata dari snapshot 01/09/2026:
                "How much does SonarQube cost?",   # pertanyaan FAQ (sonarsource)
                "Pricing",                          # judul halaman (redis)
                "Let's talk numbers",               # ajakan bicara (redis)
                "Plans", "Compare plans", "FAQ", "Contact us",
                "Images with fewer than 50,000 px",  # kalimat dokumentasi
                # Kasus nyata dari snapshot 02/09/2026:
                "Up to 15% off",      # badge promo (replit)
                "Save 20%",           # badge promo
                "by",                 # pecahan kalimat (openrouter)
                "and", "per",
                ]:
        check(f"tolak nama palsu: {bad!r}", not ok(bad))
    for good in ["Pro", "Business", "Free", "Team", "Scale", "Enterprise",
                 "Pay as you go", "Hobby",
                 # Nama sah dari data nyata — tidak boleh ikut tersaring:
                 "Essentials", "Cloud Coding Agent", "Serverless Training API",
                 "RTX PRO 6000 Max-Q", "H100 PCIE", "Pricing Pro"]:
        check(f"terima nama sah: {good!r}", ok(good))

    # Sampah di kedua sisi tidak boleh menghasilkan peristiwa apa pun.
    old = [{"name": "Pro", "amount": 20.0, "period": "month", "features": []},
           {"name": "Everything in Pro and:", "amount": 0.0, "features": []}]
    new = [{"name": "Pro", "amount": 20.0, "period": "month", "features": []}]
    check("sampah hilang dari rekaman -> nol peristiwa",
          diff_plans(old, new) == [], f"-> {diff_plans(old, new)}")

    # Penghapusan paket SUNGGUHAN tetap harus tercatat.
    real = diff_plans(
        [{"name": "Free", "amount": 0.0, "features": []},
         {"name": "Pro", "amount": 20.0, "features": []}],
        [{"name": "Pro", "amount": 20.0, "features": []}])
    check("penghapusan paket sungguhan tetap tercatat",
          any(e["type"] == "plan_removed" and e["plan"] == "Free" for e in real),
          f"-> {real}")

    # Tanda baca menggantung tidak boleh menghasilkan paket hilang + baru.
    from collector.extract import clean_plan_name
    check("nama dirapikan: 'Single Sign-On -' -> 'Single Sign-On'",
          clean_plan_name("Single Sign-On -") == "Single Sign-On")
    same = diff_plans(
        [{"name": "Single Sign-On", "amount": 150.0, "features": []}],
        [{"name": "Single Sign-On", "amount": 150.0, "features": []}])
    check("nama identik -> nol peristiwa", same == [], f"-> {same}")


def test_phantom_free() -> None:
    """"Harga tidak ditemukan" bukan "harganya nol".

    Halaman Mailchimp menghitung harganya di sisi klien, jadi sebagian
    pengambilan menghasilkan HTML tanpa angka sama sekali. Satu elemen "Free"
    yang nyasar di tabel fitur lalu membuat seluruh paket terbaca gratis, dan
    keesokan harinya tercatat sebagai penurunan harga 100%. Selama sepekan itu
    mengisi separuh metrik gerbang dengan peristiwa yang tidak pernah terjadi.
    """
    print("\n7. harga hilang bukan berarti gratis")
    html = """<!doctype html><html><body>
      <div><h3>Hobby</h3><p>Free</p><ul><li>1 project</li></ul></div>
      <div><h3>Standard</h3>
        <p>Send up to 6,000 emails each month. Need to manage more contacts?
           Get in touch to learn about custom plans.</p>
        <table><tr><td>Automations</td><td>Free</td></tr>
               <tr><td>Support</td><td>Email</td></tr></table>
      </div>
      <div><h3>Pro</h3><p>$20<span>/month</span></p><ul><li>Everything</li></ul></div>
    </body></html>"""
    res = extract.extract("acme", normalize.process(html)["soup"], html)
    by = {p["name"]: p for p in res["plans"]}
    check("kartu tanpa harga TIDAK ditebak sebagai Free",
          "Standard" not in by, f"-> {sorted(by)}")
    check("paket gratis sungguhan tetap terbaca",
          by.get("Hobby", {}).get("amount") == 0.0, f"-> {by.get('Hobby')}")
    check("paket berharga tidak terpengaruh",
          by.get("Pro", {}).get("amount") == 20.0, f"-> {by.get('Pro')}")
    check("penanda gratis di slot harga diterima",
          extract._free_in_price_slot("Hobby Free 1 project"))
    check("penanda gratis terkubur di tabel fitur ditolak",
          not extract._free_in_price_slot(
              "Standard Send up to 6,000 emails each month. Need to manage "
              "more contacts? Get in touch to learn about custom plans. "
              "Automations Free"))


def test_jsonld_only_pages() -> None:
    """Halaman yang isinya cuma cangkang SPA + JSON-LD.

    Ditemukan 05/09/2026. Arsip jira seluruhnya berbunyi
    `<div id="wac-root"></div>` — teksnya kosong, jadi content_hash-nya sama
    persis dengan bitwarden, confluence dan n8n: hash string kosong. Selama itu
    harga jira yang tercatat ($7.91 Standard, $14.54 Premium) datang dari
    JSON-LD, yang dibuang sebelum pengarsipan. Dua akibatnya:
    perubahan harga di sana tidak akan pernah terdeteksi, dan angka yang
    terlanjur dicatat tidak bisa diperiksa ulang dari arsip.
    """
    print("\n8. halaman JSON-LD tanpa teks")
    html = ('<!doctype html><html><head><script type="application/ld+json">'
            '{"@type":"Product","offers":[{"@type":"Offer","name":"Standard",'
            '"price":"7.91","priceCurrency":"USD"}]}</script>'
            '<script>var k="pscale_pw_' + "a" * 32 + '";</script>'
            '</head><body><div id="wac-root"></div></body></html>')
    proc = normalize.process(html)
    check("teks tetap kosong (JSON-LD tidak mencemari teks/hash)",
          proc["text"].strip() == "", f"-> {proc['text']!r}")
    check("halaman ditandai tipis",
          proc["text_bytes"] < config.THIN_TEXT_BYTES, f"-> {proc['text_bytes']}")
    check("JSON-LD ikut tersimpan di arsip",
          "ld+json" in proc["archive_html"] and "7.91" in proc["archive_html"])
    check("script biasa TETAP tidak diarsipkan",
          "var k=" not in proc["archive_html"])
    check("kredensial di dalam JSON-LD tetap disamarkan",
          "pscale_pw_" not in proc["archive_html"], )

    naik = normalize.process(html.replace('"7.91"', '"9.50"'))
    check("hash teks memang tidak bergerak — inilah titik butanya",
          naik["content_hash"] == proc["content_hash"])
    check("tapi arsipnya kini menyimpan bedanya",
          naik["archive_html"] != proc["archive_html"])

    # --- deteksi ------------------------------------------------------------
    base = {"content_hash": "sama", "text_bytes": 1, "_text": "",
            "plans": [{"name": "Standard", "amount": 7.91, "currency": "USD",
                       "period": "month"}], "models": []}
    tetap = dict(base, plans=[dict(base["plans"][0])])
    check("tipis + isi sama -> tetap tenang", compare(base, tetap, "") is None)

    beda = dict(base, plans=[dict(base["plans"][0], amount=9.5)])
    ch = compare(base, beda, "")
    check("tipis + harga JSON-LD bergerak -> terdeteksi",
          ch is not None and ch["kind"] == "price_change",
          f"-> {ch and ch['kind']}")

    # Halaman normal TIDAK ikut berubah perilakunya: hash teks tetap satu-satunya
    # pemicu di sana. Itu yang membuat arsip ini tenang selama ini.
    tebal = dict(base, text_bytes=50_000)
    tebal_beda = dict(beda, text_bytes=50_000)
    check("halaman normal: hash sama -> tetap tidak dianggap berubah",
          compare(tebal, tebal_beda, "") is None)


def test_parser_version_guard() -> None:
    """Pembaca angka berubah != harga berubah.

    Terjadi 05/09/2026. PRICE_RE dilebarkan dari 2 ke 6 desimal — perbaikan
    yang benar — tapi rekaman kemarin sudah tersimpan dengan pembacaan lama.
    Keesokan harinya pembanding melihat $0.00 -> $0.0045 pada halaman yang
    sama persis dan mencatatnya sebagai kenaikan harga: 26 angka palsu di 7
    halaman, tanpa satu vendor pun mengubah harga.
    """
    print("\n9. penjaga versi pembaca")
    old = {
        "parser_version": 1, "content_hash": "a", "_text": "x",
        "plans": [{"name": "Pro", "price_raw": "$0.00", "amount": 0.0,
                   "currency": "USD", "period": "month"}],
        "models": [],
    }
    new = {
        "parser_version": 2, "content_hash": "b",
        "plans": [{"name": "Pro", "price_raw": "$0.0045", "amount": 0.0045,
                   "currency": "USD", "period": "month"}],
        "models": [],
    }
    ch = compare(old, new, "y")
    check("versi pembaca beda -> BUKAN price_change",
          ch["kind"] == "parser_upgrade", f"-> {ch['kind']}")
    check("peristiwanya tetap tercatat, tidak ada yang hilang",
          any(e["type"] == "price_changed" for e in ch["plan_events"]),
          f"-> {ch['plan_events']}")
    check("versi asal & tujuan ikut dicatat",
          ch["parser_version_from"] == 1 and ch["parser_version_to"] == 2)

    same = dict(new, parser_version=1)
    check("versi pembaca sama -> perubahan harga dihitung seperti biasa",
          compare(old, same, "y")["kind"] == "price_change")

    # Rekaman lama (sebelum penanda ini ada) tidak boleh membungkam sinyal asli
    # selama sehari tanpa alasan — kasusnya sudah ditangani lewat corrections.
    tanpa_versi = {k: v for k, v in old.items() if k != "parser_version"}
    check("rekaman tanpa penanda dianggap versi sekarang",
          compare(tanpa_versi, dict(new, parser_version=config.PARSER_VERSION),
                  "y")["kind"] == "price_change")


def test_corrections_log() -> None:
    """Arsip tidak ditulis ulang; penghitungnya yang menyesuaikan."""
    print("\n10. catatan koreksi")
    path = config.CHANGES_DIR / "corrections.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"date": "2026-09-05", "slug": "voyage-ai",
                    "kind": "price_change", "reason": "uji"}) + "\n"
        + "\n"                                   # baris kosong harus diabaikan
        + "{bukan json}\n",                      # baris rusak tidak boleh crash
        encoding="utf-8")
    got = storage.load_corrections()
    check("koreksi terbaca sebagai (tanggal, slug, kind)",
          got == {("2026-09-05", "voyage-ai", "price_change")}, f"-> {got}")
    path.unlink()
    check("tanpa berkas koreksi: kosong, bukan galat",
          storage.load_corrections() == set())


def test_model_tables() -> None:
    """Harga per-model dari tabel halaman API.

    Sampai 04/09/2026 perubahan di halaman harga API selalu jatuh ke
    `catalog_change`, karena nama model terbaca sebagai nama paket:
    `gpt-6-astra` tercatat sebagai "paket baru". Padahal itu peristiwa harga
    yang sah — dan halaman-halaman inilah yang paling sering berubah.
    """
    print("\n6. tabel harga model")
    from collector.modeltable import diff_models

    html = render("modeltable.html")
    proc = normalize.process(html)
    res = extract.extract("zephyr", proc["soup"], html)
    models = {m["model"]: m for m in res["models"]}

    check("model terbaca dari tabel", set(models) ==
          {"zephyr-large", "zephyr-mini", "zephyr-embed", "zephyr-embed-lite"},
          f"-> {sorted(models)}")
    check("tabel perbandingan paket TIDAK dibaca sebagai model",
          not any(m in models for m in ("Seats", "Support")))
    # Kolom pertamanya berjudul "MODEL", jadi pemeriksaan header saja tidak
    # cukup — yang menolaknya adalah bentuk: cuma 2 dari 6 baris berisi harga.
    check("tabel yang diputar (model jadi kolom) TIDAK dibaca sebagai model",
          not any(m in models for m in
                  ("1M INPUT TOKENS", "1M OUTPUT TOKENS", "CONTEXT LENGTH")),
          f"-> {sorted(models)}")

    if "zephyr-large" in models:
        p = models["zephyr-large"]["prices"]
        check("kolom berjudul sama diberi nomor, tidak saling menimpa",
              p.get("input", {}).get("amount") == 10.0
              and p.get("input (2)", {}).get("amount") == 20.0
              and p.get("output (2)", {}).get("amount") == 75.0, f"-> {p}")
    if "zephyr-embed" in models:
        check("enam desimal utuh ($0.00012, bukan $0.00)",
              models["zephyr-embed"]["prices"]["price per thousand tokens"]["amount"]
              == 0.00012, f"-> {models['zephyr-embed']['prices']}")
        check("satuan harga terbaca dari caption",
              "thousand tokens" in models["zephyr-embed"]["unit"],
              f"-> {models['zephyr-embed']['unit']}")

    check("tabel yang dicetak dua kali tidak menggandakan model",
          len([m for m in res["models"] if m["model"] == "zephyr-large"]) == 1)

    # --- pembanding --------------------------------------------------------
    old = res["models"]
    naik = json.loads(json.dumps(old))
    for m in naik:
        if m["model"] == "zephyr-mini":
            m["prices"]["input"] = {"raw": "$0.30", "amount": 0.30}
    ev = diff_models(old, naik)
    check("kenaikan harga model terdeteksi sekali",
          len(ev) == 1 and ev[0]["type"] == "model_price_changed"
          and ev[0]["model"] == "zephyr-mini", f"-> {ev}")
    if ev and ev[0].get("changes"):
        c = ev[0]["changes"][0]
        check("persentase & arah benar (0.20 -> 0.30 = +50%)",
              c["pct_change"] == 50.0 and c["direction"] == "up", f"-> {c}")

    tambah = json.loads(json.dumps(old))
    tambah.append({"key": "zephyr-ultra", "model": "zephyr-ultra",
                   "prices": {"input": {"raw": "$30", "amount": 30.0}},
                   "currency": "USD", "unit": ""})
    types = {e["type"] for e in diff_models(old, tambah)}
    check("model baru tercatat sebagai model_added", types == {"model_added"},
          f"-> {types}")
    check("model hilang tercatat sebagai model_removed",
          {e["type"] for e in diff_models(tambah, old)} == {"model_removed"})
    check("halaman tanpa perubahan: tidak ada peristiwa",
          diff_models(old, json.loads(json.dumps(old))) == [])

    # Ini yang mencegah banjir laporan di hari pertama pemasangan: rekaman
    # kemarin belum punya kunci "models" sama sekali, jadi seluruh isi tabel
    # akan terbaca sebagai "model baru" kalau tidak dijaga.
    check("rekaman lama tanpa tabel model: DIAM, bukan banjir model baru",
          diff_models(None, old) == [], f"-> {len(diff_models(None, old))}")

    # --- klasifikasi --------------------------------------------------------
    base = {"content_hash": "a", "plans": [], "models": old, "_text": "x"}
    moved = {"content_hash": "b", "plans": [], "models": naik}
    ch = compare(base, moved, "y")
    check("harga model bergerak -> price_change", ch["kind"] == "price_change",
          f"-> {ch['kind']}")
    added = {"content_hash": "b", "plans": [], "models": tambah}
    ch2 = compare(base, added, "y")
    check("model baru saja -> catalog_change", ch2["kind"] == "catalog_change",
          f"-> {ch2['kind']}")
    lama = {"content_hash": "a2", "plans": [], "_text": "x"}   # tanpa "models"
    ch3 = compare(lama, moved, "y")
    check("hari pertama: bukan price_change palsu",
          ch3["kind"] == "page_change" and ch3["model_events"] == [],
          f"-> {ch3['kind']}")


def test_model_table_shapes() -> None:
    """Bentuk tabel harga lain yang ditemukan di arsip 06/09/2026."""
    print("\n6b. bentuk tabel harga lain")
    from collector.modeltable import _is_model_column

    check("kolom 'Base Model' dikenali", _is_model_column("base model"))
    check("kolom 'GPU Type' dikenali", _is_model_column("gpu type"))
    check("kolom 'Hardware' dikenali", _is_model_column("hardware"))
    check("kolom 'Base model parameter count' dikenali",
          _is_model_column("base model parameter count"))
    check("kolom 'Feature' DITOLAK — itu tabel perbandingan paket",
          not _is_model_column("feature"))
    check("kolom 'Model capabilities' DITOLAK",
          not _is_model_column("model capabilities"))
    check("kalimat panjang ditolak",
          not _is_model_column("compare every model across all of our plans"))

    # --- tabel tanpa baris judul (bentuk together-ai) -----------------------
    tanpa_judul = """<!doctype html><html><body><table>
      <tr><td>MiniMax M3</td><td>$0.30</td><td>$1.20</td></tr>
      <tr><td>Kimi K3</td><td>$3.00</td><td>$15.00</td></tr>
      <tr><td>Qwen3 Plus</td><td>$0.80</td><td>$2.40</td></tr>
    </table></body></html>"""
    res = extract.extract("t", normalize.process(tanpa_judul)["soup"], tanpa_judul)
    got = {m["model"] for m in res["models"]}
    check("tabel tanpa judul kolom tetap terbaca",
          got == {"MiniMax M3", "Kimi K3", "Qwen3 Plus"}, f"-> {sorted(got)}")
    check("jumlah kolom ikut ke dalam kunci",
          all(m["key"].endswith("|k3") for m in res["models"]),
          f"-> {[m['key'] for m in res['models']]}")

    # --- nama baris berulang antar tabel (bentuk halaman Gemini) -----------
    # Satu tabel per model, semua memakai baris "Input price"/"Output price".
    # Dengan kolom bernomor posisi, baris milik model berbeda bertabrakan pada
    # kunci yang sama — dan satu model baru akan menggeser SEMUA kunci lalu
    # tercatat sebagai harga bergerak. Harus ditolak seluruhnya.
    bertabrakan = """<!doctype html><html><body>
      <h3>gemini-flash</h3><table>
        <tr><td>Input price</td><td>Free of charge</td><td>$0.375</td></tr>
        <tr><td>Output price</td><td>Free of charge</td><td>$1.875</td></tr>
        <tr><td>Context caching price</td><td>Free of charge</td><td>$0.0375</td></tr>
      </table>
      <h3>gemini-pro</h3><table>
        <tr><td>Input price</td><td>Not available</td><td>$1.35</td></tr>
        <tr><td>Output price</td><td>Not available</td><td>$6.75</td></tr>
        <tr><td>Context caching price</td><td>Not available</td><td>$0.135</td></tr>
      </table>
    </body></html>"""
    res2 = extract.extract("g", normalize.process(bertabrakan)["soup"], bertabrakan)
    check("nama baris berulang antar tabel -> ditolak seluruhnya",
          res2["models"] == [], f"-> {[m['model'] for m in res2['models']]}")


def test_price_moves_need_a_moving_number() -> None:
    """Mata uang berubah tapi angkanya sama = BUKAN perubahan harga.

    07/09/2026: halaman Confluence mulai dirender JavaScript, ekstraktornya
    berpindah dari JSON-LD ke DOM, dan paket "Free" terbaca `0 (USD)` kemarin
    lalu `Free (mata uang tidak diketahui)` hari ini. Nol tetap nol — tapi
    tercatat sebagai perubahan harga. Dari 123 catatan price_changed di arsip,
    hanya inilah satu-satunya yang angkanya tidak bergerak.
    """
    print("\n6e. harga bergerak = angkanya berubah")
    sama = diff_plans(
        [{"name": "Free", "price_raw": "0", "amount": 0.0, "currency": "USD"}],
        [{"name": "Free", "price_raw": "Free", "amount": 0.0, "currency": None}])
    check("mata uang jadi tidak diketahui -> bukan peristiwa apa pun",
          sama == [], f"-> {sama}")

    pindah = diff_plans(
        [{"name": "Pro", "price_raw": "$20", "amount": 20.0, "currency": "USD"}],
        [{"name": "Pro", "price_raw": "€20", "amount": 20.0, "currency": "EUR"}])
    check("USD -> EUR dengan angka sama -> currency_changed, bukan harga",
          [e["type"] for e in pindah] == ["currency_changed"], f"-> {pindah}")

    naik = diff_plans(
        [{"name": "Pro", "price_raw": "$20", "amount": 20.0, "currency": "USD"}],
        [{"name": "Pro", "price_raw": "$25", "amount": 25.0, "currency": "USD"}])
    check("angka bergerak -> tetap price_changed",
          [e["type"] for e in naik] == ["price_changed"], f"-> {naik}")


def test_free_name_contradiction() -> None:
    """Namanya bilang gratis, angkanya bilang berbayar — kartunya salah baca.

    Ditemukan 07/09 lewat Mailchimp: "Free for 14 days" kemarin Free, hari ini
    $20. Sapuan seluruh arsip menemukan 13 kartu serupa di 8 situs — airtable
    "Free" $20, github-copilot "Free plan" $15, newrelic "Free" $49 — semuanya
    salah, dan tiap satunya bom waktu yang sama.
    """
    print("\n6f. nama gratis vs angka berbayar")
    html = """<!doctype html><html><body>
      <div><h3>Free</h3><p>$0</p><ul><li>1 project</li></ul></div>
      <div><h3>Free for 14 days</h3><p>$20<span>/month</span></p>
           <ul><li>Trial banner</li></ul></div>
      <div><h3>Freelancer</h3><p>$12<span>/month</span></p>
           <ul><li>Paket sah, bukan pernyataan gratis</li></ul></div>
      <div><h3>Pro</h3><p>$20<span>/month</span></p><ul><li>Semua</li></ul></div>
    </body></html>"""
    res = extract.extract("acme", normalize.process(html)["soup"], html)
    by = {p["name"]: p for p in res["plans"]}
    check("kartu 'Free for 14 days' seharga $20 dibuang",
          "Free for 14 days" not in by, f"-> {sorted(by)}")
    check("paket gratis sungguhan tetap ada",
          by.get("Free", {}).get("amount") == 0.0, f"-> {by.get('Free')}")
    check("'Freelancer' TIDAK ikut terbuang — batas kata dijaga",
          by.get("Freelancer", {}).get("amount") == 12.0,
          f"-> {by.get('Freelancer')}")
    check("paket berbayar biasa tidak terpengaruh",
          by.get("Pro", {}).get("amount") == 20.0)


def test_settle_ms() -> None:
    """Jeda render per-target — untuk halaman yang lambat memunculkan harga.

    Empat target (bitwarden, n8n, loom, groq) sudah memakai render: js tapi
    arsipnya tetap cangkang kosong. Menaikkan jeda global akan memperlambat
    139 target lain tanpa alasan, jadi jedanya diberikan per target.

    Buktinya diambil lewat pengambilan harian yang normal — BUKAN lewat
    permintaan tambahan. Menjalankan validator pada hari yang sama dengan
    kolektor tetap berarti dua permintaan untuk satu halaman.
    """
    print("\n6d. jeda render per-target")
    from collector.fetcher import _settle

    check("tanpa setelan -> pakai bawaan",
          _settle(None) == config.JS_SETTLE_MS, f"-> {_settle(None)}")
    check("setelan wajar dipakai apa adanya", _settle(9000) == 9000)
    check("lebih kecil dari bawaan tidak menurunkan mutu",
          _settle(100) == config.JS_SETTLE_MS, f"-> {_settle(100)}")
    check("salah ketik tidak bisa menggantung eksekusi harian",
          _settle(9_999_999) == config.JS_SETTLE_MAX,
          f"-> {_settle(9_999_999)}")
    check("nol dianggap tidak diisi", _settle(0) == config.JS_SETTLE_MS)

    live = {t.slug: t for t in config.load_targets(ROOT / "targets" / "targets.yaml")}
    salah = [t.slug for t in live.values()
             if t.settle_ms and t.render != "js"]
    check("settle_ms tidak dipasang pada target static (tidak ada gunanya)",
          not salah, f"-> {salah}")


def test_heading_tail() -> None:
    """Nama yang BERAKHIR "pricing" adalah judul bagian, bukan produk.

    Sapuan arsip 10/09/2026 menemukan 20 baris seperti ini di 12 situs —
    "On-Demand GPU Pricing" $3.99, "Usage-based pricing" $50 — tidak satu pun
    benar-benar paket. Menariknya, setelah judulnya ditolak, nama produk yang
    sesungguhnya justru terbaca: hyperstack jadi "On-Demand GPU", pinecone
    jadi "multilingual-e5-large".
    """
    print("\n6g. judul bagian berakhiran 'pricing'")
    from collector.extract import _plausible_plan_name

    for buruk in ("On-Demand GPU Pricing", "Usage pricing", "Bunny Pricing",
                  "Standard pricing", "Pay as You Go Pricing"):
        check(f"{buruk!r} ditolak", not _plausible_plan_name(buruk))
    for baik in ("Pricing Pro", "Pro", "Team", "Enterprise", "GPU+"):
        check(f"{baik!r} tetap diterima", _plausible_plan_name(baik),
              f"-> ditolak")


def test_site_builder() -> None:
    """Halaman publik: aman, jujur, dan deterministik."""
    print("\n11. pembangun halaman publik")
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_site

    # --- keamanan: isi halaman berasal dari situs luar -----------------------
    jahat = '<script>alert(1)</script>'
    out = build_site.esc(jahat)
    check("teks dari situs luar di-escape, tidak bisa menyuntik skrip",
          "<script>" not in out and "&lt;script&gt;" in out, f"-> {out}")
    check("tanda kutip ikut di-escape (aman di dalam atribut)",
          "&quot;" in build_site.esc('a"b'))
    check("None tidak meledak", build_site.esc(None) == "")

    # --- label kolom sementara tidak ditampilkan mentah ----------------------
    ringkas = build_site._harga_ringkas({
        "kolom 2": {"raw": "$0.21"}, "kolom 3": {"raw": "$0.25"}})
    check("label 'kolom N' tidak muncul di halaman publik",
          ringkas == "$0.21 · $0.25", f"-> {ringkas!r}")
    ringkas2 = build_site._harga_ringkas({"input": {"raw": "$1.00"}})
    check("label sungguhan tetap ditampilkan",
          ringkas2 == "input: $1.00", f"-> {ringkas2!r}")

    # --- persentase ---------------------------------------------------------
    check("kenaikan diberi tanda +", "+12%" in build_site.fmt_pct(12))
    check("penurunan diberi kelas 'turun'", 'class="turun"' in
          build_site.fmt_pct(-5))
    check("persentase kosong tidak merusak", build_site.fmt_pct(None) == "")

    # --- peristiwa yang sudah dikoreksi tidak boleh tampil -------------------
    ubah = [{"date": "2026-09-07", "slug": "mailchimp", "name": "Mailchimp",
             "url": "https://x.test", "kind": "price_change",
             "plan_events": [{"type": "price_changed", "plan": "Free",
                              "from": {"raw": "Free"}, "to": {"raw": "$20"}}]}]
    tanpa = build_site.recent_rows(ubah, set(), "2026-09-07")
    dengan = build_site.recent_rows(
        ubah, {("2026-09-07", "mailchimp", "price_change")}, "2026-09-07")
    check("tanpa koreksi: baris tampil", len(tanpa) == 1, f"-> {tanpa}")
    check("sudah dikoreksi: TIDAK ditampilkan ke publik",
          dengan == [], f"-> {dengan}")


def test_target_diagnosis() -> None:
    """Validator harus memberi tindakan, bukan sekadar angka."""
    print("\n6c. diagnosis target")
    sys.path.insert(0, str(ROOT / "scripts"))
    from check_targets import diagnose

    tipis = {"thin": True, "plans": 0, "tables": 0}
    check("cangkang kosong pada target static -> sarankan render: js",
          "coba render: js" in diagnose(tipis, "static"))
    check("cangkang kosong PADAHAL sudah js -> sarankan tindakan lain",
          "kandidat nonaktif" in diagnose(tipis, "js"))
    check("halaman sehat -> tanpa catatan",
          diagnose({"thin": False, "plans": 4, "tables": 0}, "static") == "")
    check("hanya tabel -> wajar untuk halaman API",
          "wajar" in diagnose({"thin": False, "plans": 0, "tables": 3}, "static"))
    check("satu paket saja -> minta periksa manual",
          "periksa manual" in
          diagnose({"thin": False, "plans": 1, "tables": 0}, "static"))


def test_one_request_per_page() -> None:
    """Satu halaman = satu permintaan per eksekusi.

    Ditemukan 04/09/2026: `anthropic-claude` (…/pricing) dan `anthropic-api`
    (…/pricing#api) adalah halaman yang sama — fragment tidak pernah dikirim
    ke server. Penjaga sekali-per-hari memakai kunci slug, jadi keduanya lolos
    dan anthropic.com/pricing diambil dua kali sehari selama sepekan.
    """
    from collector.config import Target, fetch_key
    from collector.run import dedupe_by_url

    check("fragment diabaikan saat membandingkan halaman",
          fetch_key("https://a.com/pricing#api") == fetch_key("https://a.com/pricing"))
    check("garis miring di ujung diabaikan",
          fetch_key("https://a.com/pricing/") == fetch_key("https://a.com/pricing"))
    check("nama host tidak peka huruf besar-kecil",
          fetch_key("https://A.COM/pricing") == fetch_key("https://a.com/pricing"))
    check("query TIDAK diabaikan — halaman yang beda",
          fetch_key("https://a.com/p?plan=team") != fetch_key("https://a.com/p"))
    check("path akar tetap utuh",
          fetch_key("https://a.com/") == "https://a.com/")

    def t(slug, url):
        return Target(slug=slug, name=slug, url=url)

    kept, dropped = dedupe_by_url([
        t("anthropic-claude", "https://www.anthropic.com/pricing"),
        t("anthropic-api", "https://www.anthropic.com/pricing#api"),
        t("cursor", "https://cursor.com/pricing"),
    ])
    check("halaman ganda hanya diambil sekali",
          [x.slug for x in kept] == ["anthropic-claude", "cursor"],
          f"-> {[x.slug for x in kept]}")
    check("yang pertama di daftar dipertahankan",
          dropped == [("anthropic-claude", "anthropic-api")], f"-> {dropped}")

    import collector.config as _cfg
    live = [x for x in _cfg.load_targets(ROOT / "targets" / "targets.yaml")
            if x.enabled]
    _, live_dupes = dedupe_by_url(live)
    check("targets.yaml produksi tidak punya halaman ganda",
          live_dupes == [], f"-> {live_dupes}")


def test_change_classification() -> None:
    """`price_change` hanya untuk angka yang benar-benar bergerak.

    Snapshot 02/09/2026 menunjukkan penambahan/penghapusan paket tidak bisa
    dipercaya: halaman render-JS kadang menampilkan paket kadang tidak
    (synthesia "Free" hilang, suno "Free Plan" muncul), dan baris fitur ikut
    terbaca sebagai paket. Karena `price_change` adalah metrik penentu
    gerbang bulan ke-3, add/remove dipisah ke `catalog_change`.
    """
    print("\n3d. klasifikasi jenis perubahan")

    def kind_of(old_plans, new_plans):
        old = {"content_hash": "a", "plans": old_plans, "_text": "lama"}
        new = {"content_hash": "b", "plans": new_plans}
        return compare(old, new, "baru")["kind"]

    check("angka bergerak -> price_change",
          kind_of([{"name": "Pro", "amount": 20.0, "features": []}],
                  [{"name": "Pro", "amount": 25.0, "features": []}])
          == "price_change")

    check("paket baru muncul -> catalog_change (BUKAN price_change)",
          kind_of([{"name": "Pro", "amount": 20.0, "features": []}],
                  [{"name": "Pro", "amount": 20.0, "features": []},
                   {"name": "Max", "amount": 200.0, "features": []}])
          == "catalog_change")

    check("paket hilang -> catalog_change",
          kind_of([{"name": "Pro", "amount": 20.0, "features": []},
                   {"name": "Free", "amount": 0.0, "features": []}],
                  [{"name": "Pro", "amount": 20.0, "features": []}])
          == "catalog_change")

    check("hanya fitur berubah -> plan_detail_change",
          kind_of([{"name": "Pro", "amount": 20.0, "features": ["A"]}],
                  [{"name": "Pro", "amount": 20.0, "features": ["B"]}])
          == "plan_detail_change")

    check("teks berubah tapi paket sama -> page_change",
          kind_of([{"name": "Pro", "amount": 20.0, "features": []}],
                  [{"name": "Pro", "amount": 20.0, "features": []}])
          == "page_change")

    # Peristiwanya TIDAK boleh hilang, hanya klasifikasinya yang berubah.
    old = {"content_hash": "a", "plans": [{"name": "Pro", "amount": 20.0,
                                           "features": []}], "_text": "l"}
    new = {"content_hash": "b", "plans": [{"name": "Pro", "amount": 20.0,
                                           "features": []},
                                          {"name": "Max", "amount": 200.0,
                                           "features": []}]}
    res = compare(old, new, "b")
    check("peristiwa tetap tercatat lengkap di plan_events",
          any(e["type"] == "plan_added" and e["plan"] == "Max"
              for e in res["plan_events"]), f"-> {res['plan_events']}")


def test_diff() -> None:
    print("\n3. pembanding")
    old = [
        {"name": "Pro", "price_raw": "$20", "amount": 20.0, "currency": "USD",
         "period": "month", "features": ["A", "B"]},
        {"name": "Legacy", "price_raw": "$5", "amount": 5.0, "currency": "USD",
         "period": "month", "features": []},
    ]
    new = [
        {"name": "Pro", "price_raw": "$25", "amount": 25.0, "currency": "USD",
         "period": "month", "features": ["A", "C"]},
        {"name": "Max", "price_raw": "$200", "amount": 200.0, "currency": "USD",
         "period": "month", "features": []},
    ]
    events = diff_plans(old, new)
    types = {e["type"] for e in events}
    check("kenaikan harga terdeteksi", "price_changed" in types, f"-> {types}")
    check("paket baru terdeteksi", "plan_added" in types)
    check("paket hilang terdeteksi", "plan_removed" in types)
    check("perubahan fitur terdeteksi", "features_changed" in types)

    price_ev = next(e for e in events if e["type"] == "price_changed")
    check("persentase kenaikan benar (+25%)", price_ev.get("pct_change") == 25.0,
          f"-> {price_ev.get('pct_change')}")
    check("arah perubahan = up", price_ev.get("direction") == "up")

    rec = {"content_hash": "abc", "plans": new}
    check("hash sama -> tidak ada perubahan",
          compare({"content_hash": "abc", "plans": new, "_text": "x"}, rec, "x") is None)
    first = compare(None, rec, "x")
    check("rekaman pertama ditandai first_seen", first["kind"] == "first_seen")


# --------------------------------------------------------------------------- #
class _Handler(http.server.BaseHTTPRequestHandler):
    pages: dict[str, str] = {}
    robots = "User-agent: *\nDisallow: /private/\n"

    def log_message(self, *a):  # senyapkan
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/robots.txt":
            body = self.robots.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        elif self.path in self.pages:
            body = self.pages[self.path].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve() -> tuple[socketserver.TCPServer, int]:
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def _run_collector(targets_file: Path, date: str) -> dict:
    import asyncio

    from collector.run import build_parser, main_async

    args = build_parser().parse_args(
        ["--targets", str(targets_file), "--date", date, "--force",
         "--concurrency", "3"]
    )
    asyncio.run(main_async(args))
    return storage.load_run_log(date) or {}


def test_end_to_end() -> None:
    print("\n4+5. robots.txt + end-to-end")
    _Handler.pages = {
        "/pricing": render("cards.html"),
        "/api-pricing": render("apitable.html"),
        "/private/secret-pricing": render("jsonld.html"),
    }
    srv, port = _serve()
    base = f"http://127.0.0.1:{port}"
    targets_file = _TMP / "targets.yaml"
    targets_file.write_text(
        "defaults:\n  render: static\n  currency: USD\n  enabled: true\n"
        "targets:\n"
        f"  - slug: acme\n    name: Acme AI\n    vendor: Acme\n"
        f"    category: ai-coding\n    url: {base}/pricing\n"
        f"  - slug: nimbus\n    name: Nimbus API\n    vendor: Nimbus\n"
        f"    category: ai-api\n    url: {base}/api-pricing\n"
        f"  - slug: forbidden\n    name: Terlarang\n    vendor: X\n"
        f"    category: ai-api\n    url: {base}/private/secret-pricing\n"
        f"  - slug: missing\n    name: Hilang\n    vendor: X\n"
        f"    category: ai-api\n    url: {base}/tidak-ada\n",
        encoding="utf-8",
    )

    try:
        log1 = _run_collector(targets_file, "2026-08-27")
        by_slug = {e["slug"]: e for e in log1["targets"]}

        check("robots.txt Disallow dipatuhi",
              by_slug["forbidden"]["status"] == "robots_denied",
              f"-> {by_slug['forbidden']['status']}")
        check("404 dicatat sebagai http_error tanpa menjatuhkan run",
              by_slug["missing"]["status"] == "http_error")
        check("target sehat berstatus ok",
              by_slug["acme"]["status"] == "ok" and by_slug["nimbus"]["status"] == "ok")
        check("run 1: semua target baru = first_seen",
              by_slug["acme"]["change_kind"] == "first_seen")
        check("berkas current tertulis",
              (config.CURRENT_DIR / "acme.json").exists()
              and (config.CURRENT_DIR / "acme.txt").exists())
        check("arsip HTML mentah tersimpan",
              (config.RAW_DIR / "acme" / "2026-08-27.html.gz").exists())

        # --- run 2: halaman identik (hanya nonce yang berganti) --------------
        _Handler.pages["/pricing"] = render("cards.html")
        _Handler.pages["/api-pricing"] = render("apitable.html")
        log2 = _run_collector(targets_file, "2026-08-28")
        by2 = {e["slug"]: e for e in log2["targets"]}
        check("run 2: TIDAK ada perubahan palsu",
              by2["acme"]["changed"] is False and by2["nimbus"]["changed"] is False,
              f"-> acme={by2['acme']['changed']} nimbus={by2['nimbus']['changed']}")
        check("run 2: tidak ada arsip mentah baru",
              not (config.RAW_DIR / "acme" / "2026-08-28.html.gz").exists())

        # --- run 3: harga benar-benar naik -----------------------------------
        _Handler.pages["/pricing"] = render("cards.html").replace(
            "$20<span>/month</span>", "$25<span>/month</span>")
        log3 = _run_collector(targets_file, "2026-08-29")
        by3 = {e["slug"]: e for e in log3["targets"]}
        check("run 3: perubahan harga terdeteksi",
              by3["acme"]["changed"] is True
              and by3["acme"]["change_kind"] == "price_change",
              f"-> {by3['acme'].get('change_kind')}")
        check("run 3: target yang tidak berubah tetap tenang",
              by3["nimbus"]["changed"] is False)

        lines = [json.loads(x) for x in
                 config.CHANGES_LOG.read_text(encoding="utf-8").splitlines() if x]
        price_changes = [
            ln for ln in lines
            if ln["slug"] == "acme" and ln["kind"] == "price_change"
        ]
        check("changes.jsonl berisi satu perubahan harga acme",
              len(price_changes) == 1, f"-> {len(price_changes)}")
        if price_changes:
            ev = next((e for e in price_changes[0]["plan_events"]
                       if e["type"] == "price_changed"), None)
            check("perubahan tercatat: Pro 20 -> 25",
                  ev is not None and ev["from"]["amount"] == 20.0
                  and ev["to"]["amount"] == 25.0 and ev["pct_change"] == 25.0,
                  f"-> {ev}")
            check("arsip mentah versi baru tersimpan",
                  (config.RAW_DIR / "acme" / "2026-08-29.html.gz").exists())

        # --- kontrak rekaman -------------------------------------------------
        # Diekstrak dengan benar tapi tidak ikut disimpan = fitur yang mati
        # tanpa suara. Persis itu yang terjadi pada harga per-model 04-05/09:
        # log melaporkan 34 model untuk deepinfra, tapi data/current/ tidak
        # pernah memuatnya, sehingga pembanding tidak punya bahan pembanding.
        from collector.diff import RECORD_FIELDS_USED
        tersimpan = json.loads(
            (config.CURRENT_DIR / "acme.json").read_text(encoding="utf-8"))
        hilang = [k for k in RECORD_FIELDS_USED if k not in tersimpan]
        check("setiap field yang dibaca pembanding benar-benar tersimpan",
              not hilang, f"-> hilang: {hilang}")

        # --- penjaga sekali-per-hari -----------------------------------------
        import asyncio

        from collector.run import build_parser, main_async
        args = build_parser().parse_args(
            ["--targets", str(targets_file), "--date", "2026-08-29"])
        asyncio.run(main_async(args))
        log4 = storage.load_run_log("2026-08-29")
        check("penjaga sekali-per-hari: jumlah target tidak bertambah",
              len(log4["targets"]) == 4, f"-> {len(log4['targets'])}")
    finally:
        srv.shutdown()
        srv.server_close()


def test_audit_2026_09_10() -> None:
    """Temuan audit menyeluruh 10/09/2026 — semua dikunci sebelum pernah terjadi.

    Setiap butir di bawah dibuktikan dulu pada salinan repo atau pada arsip
    sungguhan, lalu diperbaiki dengan cara yang TIDAK mengubah satu angka pun
    di arsip maupun di halaman publik hari ini (dibandingkan byte-per-byte).
    """
    print("\n12. temuan audit 10/09")
    import io
    from contextlib import redirect_stdout

    from collector.diff import _structured_key

    # --- 1. pembanding halaman tipis tidak boleh crash ----------------------
    # Dua paket bernama sama, satu currency "USD" satu None: sorted() dulu
    # melempar TypeError. 13 dari 140 rekaman nyata punya pasangan begini.
    rec = {"content_hash": "h", "text_bytes": 1, "parser_version":
           config.PARSER_VERSION, "plans": [
               {"name": "Pro", "amount": 20.0, "currency": "USD", "period": "month"},
               {"name": "Pro", "amount": 20.0, "currency": None, "period": None},
               {"name": "Free", "amount": None, "currency": None, "period": None},
               {"name": "Free", "amount": 0.0, "currency": "USD", "period": None},
           ], "models": [
               {"key": "m", "prices": {"input": {"amount": None}}},
               {"key": "m", "prices": {"input": {"amount": 1.0}}},
           ]}
    galat = None
    try:
        _structured_key(rec)
    except Exception as exc:  # noqa: BLE001
        galat = exc
    check("halaman tipis + nama ganda ber-None: tidak crash",
          galat is None, f"-> {galat!r}")
    kembar = json.loads(json.dumps(rec))
    kembar["plans"] = list(reversed(kembar["plans"]))
    check("halaman tipis, isi sama (urutan beda) -> tetap tenang",
          compare(rec, kembar, "") is None)
    naik = json.loads(json.dumps(rec))
    naik["plans"][0]["amount"] = 25.0
    check("halaman tipis, harga bergerak -> tetap terdeteksi",
          compare(rec, naik, "") is not None)

    # --- 2. parser_upgrade tidak boleh tampil / dihitung --------------------
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_site
    import gate_status

    upg = {"date": "2026-09-10", "slug": "xata", "name": "Xata",
           "url": "https://x.test", "kind": "parser_upgrade",
           "plan_events": [{"type": "price_changed", "plan": "8xlarge",
                            "from": {"raw": "$112", "amount": 112.0},
                            "to": {"raw": "$1121", "amount": 1121.0},
                            "pct_change": 900.89}],
           "model_events": []}
    asli = dict(upg, kind="price_change", slug="acme", name="Acme")
    rows = build_site.recent_rows([upg, asli], set(), "2026-09-10")
    check("parser_upgrade TIDAK tampil di tabel perubahan publik",
          [r["vendor"] for r in rows] == ["Acme"], f"-> {rows}")
    check("parser_upgrade TIDAK jadi 'perubahan terakhir'",
          ("xata", "8xlarge") not in build_site.last_moves([upg], set()))
    check("perubahan harga biasa tetap tampil",
          ("acme", "8xlarge") in build_site.last_moves([asli], set()))

    config.CHANGES_DIR.mkdir(parents=True, exist_ok=True)
    simpan = (config.CHANGES_LOG.read_text(encoding="utf-8")
              if config.CHANGES_LOG.exists() else None)
    config.CHANGES_LOG.write_text(json.dumps(upg) + "\n", encoding="utf-8")
    buf = io.StringIO()
    old_argv = sys.argv
    try:
        sys.argv = ["gate_status.py"]
        with redirect_stdout(buf):
            gate_status.main()
    finally:
        sys.argv = old_argv
        if simpan is None:
            config.CHANGES_LOG.unlink()
        else:
            config.CHANGES_LOG.write_text(simpan, encoding="utf-8")
    check("gerbang bulan ke-3 tidak menghitung parser_upgrade",
          "**0 / 100**" in buf.getvalue(), f"-> {buf.getvalue()[:300]!r}")

    # --- 3. koreksi yang disunting tangan tidak boleh menjatuhkan run -------
    path = config.CHANGES_DIR / "corrections.jsonl"
    path.write_text('[1, 2]\n"x"\n{bukan json}\n'
                    + json.dumps({"date": "2026-09-01", "slug": "a"}) + "\n",
                    encoding="utf-8")
    galat, got = None, None
    try:
        got = storage.load_corrections()
    except Exception as exc:  # noqa: BLE001
        galat = exc
    path.unlink()
    check("baris koreksi bukan-objek tidak menjatuhkan pemanggil",
          galat is None, f"-> {galat!r}")
    check("baris koreksi yang sah tetap terbaca",
          got == {("2026-09-01", "a", "price_change")}, f"-> {got}")

    # --- 4. pesan commit kosong tidak boleh membatalkan arsip ---------------
    import push_snapshot
    check("pesan kosong -> pesan cadangan (git menolak -m \"\")",
          push_snapshot.commit_message(["x", ""]) == push_snapshot.FALLBACK_MESSAGE)
    check("tanpa argumen -> pesan cadangan",
          push_snapshot.commit_message(["x"]) == push_snapshot.FALLBACK_MESSAGE)
    check("pesan normal dipakai apa adanya",
          push_snapshot.commit_message(["x", "data: snapshot 2026-09-10"])
          == "data: snapshot 2026-09-10")


class _Handler429(http.server.BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, *a):  # senyapkan
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/robots.txt":
            body, code = b"User-agent: *\nAllow: /\n", 200
        else:
            type(self).hits += 1
            body, code = b"slow down", 429
        self.send_response(code)
        if code == 429:
            self.send_header("Retry-After", "3600")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_refusal_is_final_for_the_day() -> None:
    """429/4xx = kembali besok. Bukan diulang, bukan dicoba lagi sore.

    Riwayat 27/08-02/09/2026: devin & windsurf menjawab 429 setiap hari.
    Kode lama mengulang dua kali per eksekusi (Retry-After dibatasi 120 detik
    walau server meminta satu jam), lalu run cadangan sore mengulang lagi —
    sampai 6 permintaan per hari ke halaman yang sudah menolak, dan tidak
    sekali pun berhasil. Aturan kita: maksimal SATU permintaan per halaman
    per hari, termasuk permintaan yang ditolak.
    """
    print("\n13. penolakan server berlaku untuk sehari")
    import asyncio

    from collector.run import build_parser, main_async

    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler429)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    targets_file = _TMP / "targets-429.yaml"
    targets_file.write_text(
        "targets:\n"
        f"  - slug: sibuk\n    name: Sibuk\n    vendor: X\n"
        f"    category: ai-api\n    url: http://127.0.0.1:{port}/pricing\n",
        encoding="utf-8")
    try:
        _Handler429.hits = 0
        args = build_parser().parse_args(
            ["--targets", str(targets_file), "--date", "2026-09-10"])
        asyncio.run(main_async(args))
        log = storage.load_run_log("2026-09-10")
        e = {x["slug"]: x for x in log["targets"]}["sibuk"]
        check("429 dicatat sebagai http_error",
              e["status"] == "http_error" and e["http_status"] == 429,
              f"-> {e.get('status')} {e.get('http_status')}")
        check("429 TIDAK diulang dalam eksekusi yang sama",
              _Handler429.hits == 1, f"-> {_Handler429.hits} permintaan")
        check("Retry-After ikut dicatat untuk pemeriksa",
              "3600" in (e.get("reason") or ""), f"-> {e.get('reason')}")

        # run cadangan sore, tanggal sama, tanpa --force
        asyncio.run(main_async(args))
        check("run cadangan sore TIDAK meminta ulang halaman yang menolak",
              _Handler429.hits == 1, f"-> {_Handler429.hits} permintaan")

        # --force tetap bisa dipakai pemilik secara sadar
        forced = build_parser().parse_args(
            ["--targets", str(targets_file), "--date", "2026-09-10", "--force"])
        asyncio.run(main_async(forced))
        check("--force tetap mengulang (keputusan manusia, bukan otomatis)",
              _Handler429.hits == 2, f"-> {_Handler429.hits} permintaan")
    finally:
        srv.shutdown()
        srv.server_close()

    from collector.run import _refused_today
    check("galat jaringan TETAP boleh dicoba sore (bukan penolakan)",
          not _refused_today({"status": "network_error", "http_status": None}))
    check("HTTP 503 TETAP boleh dicoba sore (gangguan sementara)",
          not _refused_today({"status": "http_error", "http_status": 503}))
    check("HTTP 403 dianggap penolakan untuk hari ini",
          _refused_today({"status": "http_error", "http_status": 403}))


def test_empat_digit_tanpa_koma() -> None:
    """Harga >= 4 digit tanpa koma pernah terpotong di digit ke-3 (15/09/2026).

    Gejala: `$1500` terbaca 150, `$ 1121` terbaca 112, `$1343` terbaca 134.
    Sebabnya alternatif pertama PRICE_RE memakai `(?:,\d{3})*` — cocok TANPA
    koma sama sekali — dan regex mengambil alternatif pertama yang cocok.
    Tiga angka salah ikut terbit di halaman publik: Xata 8xlarge $112 (padahal
    $1121, sesuai $1,536/jam x 730), Paperspace V100 $134 (padahal $1343),
    Synthesia Studio Avatars $100 (padahal $1000).

    Yang paling berbahaya bukan angkanya salah, melainkan dua akibatnya:
    perubahan notasi `$1343` -> `$1,343` terbaca sebagai lonjakan +902%, dan
    kenaikan sungguhan `$1343` -> `$1349` tidak terlihat sama sekali karena
    keduanya terbaca 134.
    """
    print("\n14. harga empat digit tanpa koma")
    from collector.extract import parse_price

    for teks, harap in [
        ("$1500/mo", 1500.0),        # dulu 150
        ("$ 1121", 1121.0),          # dulu 112  (Xata)
        ("$1343 / month", 1343.0),   # dulu 134  (Paperspace)
        ("$1000/year", 1000.0),      # dulu 100  (Synthesia)
        ("$2000.50", 2000.5),        # dulu 200,5
        ("$12345", 12345.0),         # dulu 123
    ]:
        check(f"{teks!r} terbaca utuh", parse_price(teks)[0] == harap,
              f"-> {parse_price(teks)[0]}")

    # Yang sudah benar TIDAK BOLEH berubah — ini syarat "meningkatkan, bukan
    # merusak". Termasuk harga API per-token yang halus (PARSER_VERSION 2).
    for teks, harap in [
        ("$1,500/mo", 1500.0),
        ("$1,234,567", 1234567.0),
        ("$150", 150.0),
        ("$12.50", 12.5),
        ("$0.000012", 0.000012),
        ("Rp 150", 150.0),
        ("$1.5k", 1500.0),           # sufiks k tetap dikali seribu
        ("$0", 0.0),
    ]:
        check(f"{teks!r} tetap seperti sebelumnya", parse_price(teks)[0] == harap,
              f"-> {parse_price(teks)[0]}")

    # Perubahan NOTASI bukan perubahan harga.
    lama = [{"name": "Growth", "amount": parse_price("$1343")[0],
             "price_raw": "$1343"}]
    baru = [{"name": "Growth", "amount": parse_price("$1,343")[0],
             "price_raw": "$1,343"}]
    ev = [e for e in diff_plans(lama, baru) if e["type"] == "price_changed"]
    check("notasi $1343 -> $1,343 BUKAN perubahan harga", ev == [], f"-> {ev}")

    # Kenaikan sungguhan yang dulu tak terlihat, kini terbaca.
    naik = [{"name": "Growth", "amount": parse_price("$1349")[0],
             "price_raw": "$1349"}]
    ev = [e for e in diff_plans(lama, naik) if e["type"] == "price_changed"]
    check("kenaikan $1343 -> $1349 kini terdeteksi", len(ev) == 1, f"-> {ev}")

    # Penanda versi pembaca WAJIB ikut naik, kalau tidak hari pertama setelah
    # perbaikan ini akan mencatat 3 "kenaikan harga" palsu (§10.6).
    check("PARSER_VERSION dinaikkan bersama perubahan pembaca",
          config.PARSER_VERSION >= 4, f"-> {config.PARSER_VERSION}")


def test_halaman_publik_16_09() -> None:
    """Mutu tampilan halaman publik — diperbaiki 16/09/2026.

    Empat keluhan nyata dari membaca halaman itu sebagai pengunjung:
      * 180 dari 185 baris "perubahan harga" adalah sewa GPU, sehingga empat
        perubahan software yang justru jadi inti proyek ini tenggelam;
      * 9 baris Paperspace bertanda `period: month` tampil di bawah judul
        "Sewa GPU per jam" — angkanya benar, penyajiannya berbohong;
      * DeepInfra 07/09 tampil DUA KALI (sekali sebagai paket, sekali sebagai
        baris tabel model) dengan angka yang sama persis;
      * judul bagian seperti "Storage Pricing" terbaca sebagai produk.
    """
    print("\n15. mutu halaman publik")
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_site

    from collector.config import Target

    # --- satuan ditulis apa adanya, yang kosong tidak ditebak ---------------
    check("period 'hour' -> 'per hour'", build_site.periode_label("hour") == "per hour")
    check("period 'month' -> 'per month'",
          build_site.periode_label("month") == "per month")
    check("tanpa period -> 'not stated', BUKAN ditebak per jam",
          build_site.periode_label(None) == "not stated")

    # --- baris sewa: periode ikut, judul bagian disaring --------------------
    rec = {"slug": "gpu-uji", "plans": [
        {"name": "H100 SXM", "amount": 2.5, "price_raw": "$2.50", "period": "hour"},
        {"name": "GPU+", "amount": 298.0, "price_raw": "$298", "period": "month"},
        {"name": "Storage Pricing", "amount": 0.1, "price_raw": "$0.10",
         "period": "hour"},                      # judul bagian, bukan produk
        {"name": "Tanpa harga", "amount": None, "price_raw": ""},
    ]}
    (config.CURRENT_DIR).mkdir(parents=True, exist_ok=True)
    (config.CURRENT_DIR / "gpu-uji.json").write_text(
        json.dumps(rec), encoding="utf-8")
    targets = {"gpu-uji": Target(slug="gpu-uji", name="GPU Uji",
                                 url="https://gpu.test/pricing",
                                 category="gpu-rental")}
    rows = build_site.gpu_rows(targets, {})
    nama = [r["item"] for r in rows]
    check("judul bagian 'Storage Pricing' tidak tampil di halaman publik",
          "Storage Pricing" not in nama, f"-> {nama}")
    check("kartu sungguhan tetap tampil", "H100 SXM" in nama, f"-> {nama}")
    check("baris tanpa angka tidak tampil", "Tanpa harga" not in nama)
    satuan = {r["item"]: r["satuan"] for r in rows}
    check("harga bulanan TIDAK diberi label per jam",
          satuan.get("GPU+") == "per month", f"-> {satuan}")
    check("baris per jam tetap per jam", satuan.get("H100 SXM") == "per hour")
    (config.CURRENT_DIR / "gpu-uji.json").unlink()

    # --- satu pergerakan, satu baris ---------------------------------------
    kembar = [
        {"date": "2026-09-07", "vendor": "DeepInfra", "url": "https://d.test",
         "item": "DeepSeek-V4", "dari": "$0.08", "ke": "$0.06", "pct": -25},
        {"date": "2026-09-07", "vendor": "DeepInfra", "url": "https://d.test",
         "item": "DeepSeek-V4 · $ per 1m input tokens", "dari": "$0.08",
         "ke": "$0.06", "pct": -25},
    ]
    sisa = build_site._buang_kembar(kembar)
    check("baris kembar (paket + baris tabel) tampil sekali saja",
          len(sisa) == 1, f"-> {sisa}")
    check("yang dipertahankan adalah yang menyebut kolom harganya",
          " · " in sisa[0]["item"], f"-> {sisa[0]['item']}")
    beda = build_site._buang_kembar([
        kembar[0], dict(kembar[0], ke="$0.07")])
    check("angka berbeda TIDAK ikut dibuang", len(beda) == 2, f"-> {beda}")

    # --- software vs GPU dipisah -------------------------------------------
    ubah = [
        {"date": "2026-09-16", "slug": "vast-ai", "name": "Vast.ai",
         "url": "https://v.test", "kind": "price_change", "plan_events": [
             {"type": "price_changed", "plan": "H100", "from": {"raw": "$1"},
              "to": {"raw": "$2"}}]},
        {"date": "2026-09-16", "slug": "airbyte", "name": "Airbyte",
         "url": "https://a.test", "kind": "price_change", "plan_events": [
             {"type": "price_changed", "plan": "Standard", "from": {"raw": "$10"},
              "to": {"raw": "$20"}}]},
    ]
    rows = build_site.recent_rows(ubah, set(), "2026-09-16", {"vast-ai"})
    sw = [r for r in rows if not r["gpu"]]
    gpu = [r for r in rows if r["gpu"]]
    check("perubahan software terpisah dari GPU",
          [r["vendor"] for r in sw] == ["Airbyte"]
          and [r["vendor"] for r in gpu] == ["Vast.ai"], f"-> {rows}")

    # --- tanggal yang ditampilkan = kapan terakhir MENGAMBIL ----------------
    (config.RUNS_DIR).mkdir(parents=True, exist_ok=True)
    (config.RUNS_DIR / "2026-09-16.json").write_text("{}", encoding="utf-8")
    check("tanggal diambil dari log eksekusi, bukan log perubahan",
          build_site.tanggal_rekaman_terakhir("2026-09-01") == "2026-09-16")
    (config.RUNS_DIR / "2026-09-16.json").unlink()
    kosong = _TMP / "runs-kosong"
    kosong.mkdir(exist_ok=True)
    asli = config.RUNS_DIR
    try:
        config.RUNS_DIR = kosong
        check("tanpa log eksekusi: pakai cadangan, bukan meledak",
              build_site.tanggal_rekaman_terakhir("2026-09-01") == "2026-09-01")
    finally:
        config.RUNS_DIR = asli

    # --- kewajiban hukum & kepercayaan di halaman --------------------------
    halaman = build_site.build_html(
        tanggal="2026-09-16", hari=21, halaman=135, angka=5,
        gpu=[], gpu_lain=[], model=[], lain=[], terbaru=[], terbaru_gpu=[],
        repo="https://github.com/szeto214/ai-pricing-tracker", parser_version=4)
    check("halaman menyatakan merek dagang milik pemiliknya",
          "belong to their respective owners" in halaman
          and "not affiliated with" in halaman)
    check("halaman menyediakan jalur lapor kesalahan",
          "issues/new" in halaman, "-> tautan laporan hilang")
    check("halaman menyebut tanggalnya UTC", "Dates in UTC" in halaman)
    check("halaman menyebut versi pembaca angka",
          "parser version: 4" in halaman)
    check("halaman tetap menyatakan tidak ada pelacakan",
          "no tracking" in halaman)


def test_halaman_per_tool_17_09() -> None:
    """Satu halaman per tool — dikerjakan 17/09/2026.

    Alasannya bukan kosmetik. Statistik GitHub 14 hari (diperiksa 16/09)
    menunjukkan 1 pengunjung unik, dan itu pemiliknya sendiri. Halaman utama
    tidak akan pernah muncul untuk pencarian "cursor pricing history",
    sedangkan halaman yang KHUSUS membahas satu tool bisa. Arsipnya sudah
    mampu merekonstruksi riwayat per item — 62 item punya riwayat, Vast.ai
    sampai 15 titik dalam 22 hari — dan sampai hari ini semua itu terkubur
    di satu tabel besar bercampur 135 tool lain.

    Syarat yang dikunci di sini: halaman tool TIDAK boleh memuat tanggal hari
    ini (kalau tidak, 135 berkas berubah tiap hari tanpa menambah informasi),
    tidak boleh memuat angka yang tidak ada di arsip, dan target yang
    dinonaktifkan tidak boleh meninggalkan halaman yatim.
    """
    print("\n16. halaman per tool + sitemap")
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_site

    from collector.config import Target

    # --- mata uang tidak boleh hilang, tapi juga tidak boleh dikarang ------
    check("angka telanjang + USD -> $ (dua-duanya dari rekaman yang sama)",
          build_site.uang("40", "USD") == "$40")
    check("yang sudah bersimbol tidak diberi simbol dua kali",
          build_site.uang("$40", "USD") == "$40")
    check("mata uang tidak diketahui -> apa adanya, tidak ditebak",
          build_site.uang("40", None) == "40")
    check("GBP dan EUR ikut dikenali",
          build_site.uang("13", "GBP") == "£13"
          and build_site.uang("16", "EUR") == "€16")
    check("mata uang tak bersimbol ditulis kodenya",
          build_site.uang("500", "SGD") == "500 SGD")

    # --- riwayat per tool: dikoreksi & parser_upgrade tidak ikut -----------
    ubah = [
        {"date": "2026-09-03", "slug": "acme", "name": "Acme", "url": "u",
         "kind": "price_change", "plan_events": [
             {"type": "price_changed", "plan": "Pro",
              "from": {"raw": "$20"}, "to": {"raw": "$25"}, "pct_change": 25}]},
        {"date": "2026-09-05", "slug": "acme", "name": "Acme", "url": "u",
         "kind": "price_change", "plan_events": [
             {"type": "price_changed", "plan": "Pro",
              "from": {"raw": "$25"}, "to": {"raw": "$30"}, "pct_change": 20}]},
        {"date": "2026-09-06", "slug": "acme", "name": "Acme", "url": "u",
         "kind": "parser_upgrade", "plan_events": [      # hari pembaca naik
             {"type": "price_changed", "plan": "Pro",
              "from": {"raw": "$30"}, "to": {"raw": "$300"}}]},
        {"date": "2026-09-07", "slug": "acme", "name": "Acme", "url": "u",
         "kind": "price_change", "plan_events": [        # nanti dikoreksi
             {"type": "price_changed", "plan": "Pro",
              "from": {"raw": "$30"}, "to": {"raw": "$0"}}]},
    ]
    riwayat = build_site.riwayat_per_slug(
        ubah, {("2026-09-07", "acme", "price_change")})
    tgl = [r["date"] for r in riwayat.get("acme", [])]
    check("riwayat tool: terbaru dulu", tgl == ["2026-09-05", "2026-09-03"],
          f"-> {tgl}")
    check("hari kenaikan versi pembaca TIDAK masuk riwayat publik",
          "2026-09-06" not in tgl, f"-> {tgl}")
    check("peristiwa yang sudah dikoreksi TIDAK masuk riwayat publik",
          "2026-09-07" not in tgl, f"-> {tgl}")

    # --- isi halaman --------------------------------------------------------
    rec = {"slug": "acme", "plans": [
        {"name": "Pro", "amount": 30.0, "price_raw": "30", "currency": "USD",
         "period": "month"},
        {"name": "Pricing", "amount": 1.0, "price_raw": "$1"},   # judul bagian
    ], "models": []}
    t = Target(slug="acme", name="Acme AI", url="https://acme.test/pricing",
               category="ai-coding")
    halaman = build_site.tool_page(t, rec, riwayat["acme"], "2026-08-27",
                                   "https://github.com/u/r", 4)
    check("judul halaman menyebut nama toolnya (ini yang dicari di mesin pencari)",
          "<title>Acme AI pricing history" in halaman)
    check("harga tampil dengan mata uangnya", ">$30<" in halaman, "-> $30 hilang")
    check("judul bagian tidak dipajang sebagai produk",
          ">Pricing<" not in halaman)
    check("menautkan balik ke halaman harga resmi",
          'href="https://acme.test/pricing"' in halaman)
    check("punya canonical supaya tidak dianggap halaman ganda",
          '<link rel="canonical"' in halaman)
    check("menyatakan merek dagang milik pemiliknya",
          "belong to their respective owners" in halaman
          and "not affiliated with" in halaman)
    check("menyediakan jalur lapor kesalahan", "issues/new" in halaman)
    hari_ini = dt.date.today().isoformat()
    check("TIDAK memuat tanggal hari ini (supaya tidak berubah tiap hari)",
          hari_ini not in halaman, f"-> {hari_ini} muncul di halaman")

    kosong = build_site.tool_page(t, rec, [], "2026-08-27",
                                  "https://github.com/u/r", 4)
    check("tool yang belum pernah berubah harga: dijawab jujur, bukan kosong",
          "No price change recorded" in kosong)

    # --- kumpulan halaman + sitemap ----------------------------------------
    (config.CURRENT_DIR).mkdir(parents=True, exist_ok=True)
    (config.CURRENT_DIR / "acme.json").write_text(json.dumps(rec), encoding="utf-8")
    (config.CURRENT_DIR / "mati.json").write_text(json.dumps(rec), encoding="utf-8")
    targets = {
        "acme": t,
        "mati": Target(slug="mati", name="Sudah Nonaktif",
                       url="https://mati.test/pricing", enabled=False),
    }
    berkas = build_site.build_pages(targets, ubah, set(), "2026-08-27",
                                    "2026-09-16")
    check("satu halaman untuk tiap target aktif", "t/acme.html" in berkas)
    check("target nonaktif TIDAK dibuatkan halaman", "t/mati.html" not in berkas)
    check("sitemap ikut dihasilkan", "sitemap.xml" in berkas)
    sm = berkas["sitemap.xml"]
    check("sitemap memuat halaman tool", "/t/acme.html" in sm, f"-> {sm[:200]}")
    check("sitemap memuat halaman utama", "<loc>https://" in sm)
    check("sitemap tidak memuat halaman target nonaktif", "/t/mati.html" not in sm)
    (config.CURRENT_DIR / "acme.json").unlink()
    (config.CURRENT_DIR / "mati.json").unlink()

    # --- daftar tool di halaman utama (jalan masuk mesin pencari) ----------
    daftar = build_site.daftar_tool(targets, riwayat)
    # --- tabel harus terbaca di ponsel ------------------------------------
    # 18/09/2026: diukur pada layar 390px, SEMUA tabel memotong kolom
    # harganya. Halaman "CodeRabbit" hanya memperlihatkan tanggal dan nama
    # item; Dari/Ke/Selisih ada di luar layar. Pengunjung dari Google
    # (mayoritas ponsel) tidak pernah melihat satu angka pun lalu pergi.
    tbl = build_site.table(["Date (UTC)", "Item", "From"],
                           [["2026-09-03", ("CodeRabbit Agent", "wrap-ok"),
                             "$0.50"]])
    for label in ("Date (UTC)", "Item", "From"):
        check(f"tiap sel membawa label kolomnya ({label})",
              f'data-label="{build_site.esc(label)}"' in tbl, f"-> {tbl[:200]}")
    check("kelas kolom tetap dipakai", 'class="wrap-ok"' in tbl)
    check("CSS mengubah baris jadi kartu di layar sempit",
          "@media(max-width:640px)" in build_site.CSS
          and "content:attr(data-label)" in build_site.CSS)
    check("tabel kosong tetap menjawab, bukan tabel hampa",
          build_site.table(["a"], [], kosong="Nothing yet.")
          == '<p class="dim">Nothing yet.</p>')

    # --- halaman utama tidak boleh jadi gudang -----------------------------
    banyak = [{"date": "2026-09-16", "vendor": "V", "url": "u",
               "item": f"item {i}", "dari": "$1", "ke": "$2", "pct": 1,
               "gpu": True} for i in range(50)]
    tampil, sisa = build_site.potong(banyak, build_site.MAX_BARIS_INDEKS)
    check("baris berlebih dipotong di halaman utama",
          len(tampil) == build_site.MAX_BARIS_INDEKS and sisa == 50 - len(tampil),
          f"-> {len(tampil)}, sisa {sisa}")
    check("jumlah yang tidak ditampilkan SELALU disebutkan, tidak dihilangkan diam-diam",
          "50" in build_site.catatan_sisa(sisa, 50))
    check("kalau muat semua, tidak ada catatan menggantung",
          build_site.potong([1, 2], 40) == ([1, 2], 0)
          and build_site.catatan_sisa(0, 2) == "")

    # --- verifikasi Search Console harus bertahan tiap kali dibangun ulang -
    check("token verifikasi Search Console terpasang di halaman utama",
          build_site.GOOGLE_SITE_VERIFICATION
          and build_site.meta_verifikasi()
          == '<meta name="google-site-verification" content='
             f'"{build_site.GOOGLE_SITE_VERIFICATION}">',
          f"-> {build_site.meta_verifikasi()}")
    utama = build_site.build_html(
        tanggal="2026-09-17", hari=22, halaman=135, angka=5,
        gpu=[], gpu_lain=[], model=[], lain=[], terbaru=[], terbaru_gpu=[],
        repo="https://github.com/u/r", parser_version=4, daftar="")
    check("tag verifikasi ikut tertulis di halaman yang dibangun",
          build_site.GOOGLE_SITE_VERIFICATION in utama)
    asli = build_site.GOOGLE_SITE_VERIFICATION
    try:
        build_site.GOOGLE_SITE_VERIFICATION = ""
        check("tanpa token: tidak ada tag kosong yang menggantung",
              build_site.meta_verifikasi() == "")
    finally:
        build_site.GOOGLE_SITE_VERIFICATION = asli

    check("kategori diberi label yang dimengerti pembaca",
          build_site.label_kategori("ai-api") == "AI model APIs")
    check("kategori yang belum punya label tampil apa adanya, bukan hilang",
          build_site.label_kategori("kategori-baru") == "kategori-baru")
    check("halaman utama menautkan halaman tool", 'href="t/acme.html"' in daftar)
    check("target nonaktif tidak ikut ditautkan", "t/mati.html" not in daftar)


def test_feed_dan_penyaring_18_09() -> None:
    """Alasan untuk kembali — RSS, penyaring tool, gerbang tiga lapis (18/09).

    Sampai hari ini tidak ada satu pun cara bagi pembaca untuk tahu ada
    perubahan harga tanpa membuka situsnya sendiri setiap hari. RSS dipilih
    karena cocok dengan syarat pemilik sejak hari pertama: tanpa akun, tanpa
    alamat email, tanpa "pelanggan yang harus dilayani".

    Yang dikunci di sini: feed tidak boleh berubah kalau arsipnya tidak
    berubah (kalau memakai jam dinding, tiap hari muncul diff palsu), feed
    software tidak boleh tenggelam oleh GPU, dan peristiwa yang sudah
    dikoreksi maupun `parser_upgrade` tidak boleh ikut tersiar.
    """
    print("\n17. feed RSS, penyaring tool, gerbang tiga lapis")
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_site

    ubah = [
        {"date": "2026-09-16", "slug": "airbyte", "name": "Airbyte",
         "kind": "price_change", "plan_events": [
             {"type": "price_changed", "plan": "Standard",
              "from": {"raw": "10.00", "currency": "USD"},
              "to": {"raw": "20.00", "currency": "USD"}, "pct_change": 100}]},
        {"date": "2026-09-16", "slug": "vast-ai", "name": "Vast.ai",
         "kind": "price_change", "plan_events": [
             {"type": "price_changed", "plan": "H100",
              "from": {"raw": "$1"}, "to": {"raw": "$2"}, "pct_change": 100}]},
        {"date": "2026-09-15", "slug": "acme", "name": "Acme",
         "kind": "parser_upgrade", "plan_events": [
             {"type": "price_changed", "plan": "Pro",
              "from": {"raw": "$1"}, "to": {"raw": "$9"}}]},
        {"date": "2026-09-14", "slug": "acme", "name": "Acme",
         "kind": "price_change", "plan_events": [
             {"type": "price_changed", "plan": "Pro",
              "from": {"raw": "$1"}, "to": {"raw": "$3"}}]},
    ]
    koreksi = {("2026-09-14", "acme", "price_change")}
    sw = build_site.isi_feed(ubah, koreksi, {"vast-ai"}, gpu=False)
    gpu = build_site.isi_feed(ubah, koreksi, {"vast-ai"}, gpu=True)
    check("feed software tidak memuat sewa GPU",
          [b["slug"] for b in sw] == ["airbyte"], f"-> {[b['slug'] for b in sw]}")
    check("feed GPU memuat GPU saja",
          [b["slug"] for b in gpu] == ["vast-ai"], f"-> {gpu}")
    check("hari kenaikan versi pembaca tidak ikut tersiar",
          all(b["date"] != "2026-09-15" for b in sw + gpu))
    check("peristiwa yang sudah dikoreksi tidak ikut tersiar",
          all(b["slug"] != "acme" for b in sw), f"-> {sw}")

    xml = build_site.feed(sw, judul="Uji", keterangan="Uji", jalur_diri="feed.xml")
    check("feed berupa XML yang sah", _xml_sah(xml), f"-> {xml[:200]}")
    check("butir feed menautkan ke halaman tool",
          "t/airbyte.html" in xml)
    check("angka lama dan baru ikut di isi feed",
          "$10.00" in xml and "$20.00" in xml, f"-> {xml[:400]}")
    check("tanggal RSS dibentuk dari tanggal arsip, bukan jam dinding",
          build_site.waktu_rfc822("2026-09-16")
          == "Wed, 16 Sep 2026 00:00:00 +0000",
          f"-> {build_site.waktu_rfc822('2026-09-16')}")
    check("membangun feed dua kali menghasilkan berkas identik",
          xml == build_site.feed(sw, judul="Uji", keterangan="Uji",
                                 jalur_diri="feed.xml"))

    # --- penyaring daftar tool -------------------------------------------
    from collector.config import Target
    targets = {"cursor": Target(slug="cursor", name="Cursor",
                                url="https://c.test/pricing",
                                category="ai-coding")}
    daftar = build_site.daftar_tool(targets, {})
    check("daftar tool bisa disaring (tiap tool membawa namanya)",
          'data-nama="cursor"' in daftar, f"-> {daftar[:200]}")
    check("kotak pencarian disembunyikan sampai JavaScript menyalakannya",
          'id="cari"' in daftar and "hidden" in daftar)
    check("tanpa JavaScript daftarnya tetap utuh (tidak disembunyikan CSS)",
          ".tool[hidden]" in build_site.CSS and ".tool{display:inline-block"
          in build_site.CSS)


def test_gerbang_tiga_lapis() -> None:
    """Gerbang dilaporkan tiga lapis, tidak dijumlahkan (keputusan 15/09).

    Paket software, harga per-baris API, dan sewa GPU bergerak dengan laju
    yang sama sekali berbeda. Menjumlahkannya membuat GPU — yang bergerak
    nyaris tiap hari — menutupi dua lapis lain, dan gerbangnya kehilangan
    arti persis seperti sebelum 03/09.
    """
    print("\n17b. gerbang tiga lapis")
    import io
    from contextlib import redirect_stdout

    sys.path.insert(0, str(ROOT / "scripts"))
    import gate_status

    simpan = (config.CHANGES_LOG.read_text(encoding="utf-8")
              if config.CHANGES_LOG.exists() else None)
    baris = [
        {"date": "2026-09-16", "slug": "acme", "name": "Acme",
         "kind": "price_change", "plan_events": [
             {"type": "price_changed", "plan": "Pro", "from": {"raw": "$1"},
              "to": {"raw": "$2"}}], "model_events": []},
        {"date": "2026-09-16", "slug": "nimbus", "name": "Nimbus",
         "kind": "price_change", "plan_events": [], "model_events": [
             {"type": "model_price_changed", "model": "m1", "changes": [
                 {"field": "input", "from": {"raw": "$1"}, "to": {"raw": "$2"}},
                 {"field": "output", "from": {"raw": "$3"}, "to": {"raw": "$4"}}]}]},
    ]
    config.CHANGES_DIR.mkdir(parents=True, exist_ok=True)
    config.CHANGES_LOG.write_text(
        "\n".join(json.dumps(b) for b in baris) + "\n", encoding="utf-8")
    buf, old = io.StringIO(), sys.argv
    try:
        sys.argv = ["gate_status.py"]
        with redirect_stdout(buf):
            gate_status.main()
    finally:
        sys.argv = old
        if simpan is None:
            config.CHANGES_LOG.unlink()
        else:
            config.CHANGES_LOG.write_text(simpan, encoding="utf-8")
    keluaran = buf.getvalue()
    check("paket software dilaporkan sendiri",
          "**Paket software**: **1 / 100**" in keluaran, f"-> {keluaran[:400]}")
    check("harga per-baris API dilaporkan sendiri (2 angka)",
          "**Harga per-baris API**" in keluaran and "(2 angka)" in keluaran,
          f"-> {keluaran[:400]}")
    check("sewa GPU tetap lapis terpisah", "**Sewa GPU**" in keluaran)
    check("ketiganya tidak dijumlahkan jadi satu angka",
          "TIDAK dijumlahkan" in keluaran)


def _xml_sah(teks: str) -> bool:
    import xml.dom.minidom
    try:
        xml.dom.minidom.parseString(teks)
        return True
    except Exception:  # noqa: BLE001
        return False


def test_kartu_berisi_tabel_18_09() -> None:
    """Judul bagian yang memuat tabel harga bukan paket (18/09/2026).

    Kartu seperti itu mengambil angka dari BARIS PERTAMA tabelnya, jadi ia
    bergerak setiap kali vendor menambah baris, menghapus baris, atau menukar
    kolom — padahal tidak ada harga yang berubah. Fireworks AI melakukannya
    tiga kali dengan kartu yang sama:

      01/09  $0.66 -> $1.86   baris pertama tabel dihapus vendor
      16/09  $1.86 -> $4.86   model baru disisipkan di baris teratas
      18/09  $7.00 -> $0.134  kolom tabel berubah jadi per-menit

    Ketiganya tercatat sebagai perubahan harga software, ketiganya palsu, dan
    ketiganya harus dikoreksi belakangan. Pemeriksaannya STRUKTURAL: kalau
    harga kartu hanya ada DI DALAM tabelnya, itu bagian halaman, bukan paket.
    Angkanya tidak hilang — tabelnya tetap dibaca terpisah sebagai baris model.
    """
    print("\n18. kartu judul bagian yang memuat tabel")
    from bs4 import BeautifulSoup

    from collector.extract import extract_dom

    html = """
    <body>
      <section>
        <h2>On demand deployments</h2>
        <table>
          <tr><th>GPU Type</th><th>Price ($) per minute</th></tr>
          <tr><td>H100 80 GB GPU</td><td>$0.134</td></tr>
          <tr><td>B200 180 GB GPU</td><td>$0.217</td></tr>
        </table>
      </section>
      <div>
        <h3>Pro</h3>
        <p>$20 / month</p>
        <table>
          <tr><th>Feature</th><th>Included</th></tr>
          <tr><td>Seats</td><td>5</td></tr>
        </table>
      </div>
    </body>"""
    plans = extract_dom(BeautifulSoup(html, "lxml"))
    nama = [p.name for p in plans]
    check("judul bagian berisi tabel TIDAK jadi paket",
          "On demand deployments" not in nama, f"-> {nama}")
    check("paket sungguhan yang harganya di luar tabel tetap terbaca",
          "Pro" in nama, f"-> {nama}")
    harga = {p.name: p.amount for p in plans}
    check("harga paket sungguhan tidak ikut berubah",
          harga.get("Pro") == 20.0, f"-> {harga}")

    # Kartu yang ditolak tetap menghabiskan kuota kartu. Tanpa ini, penolakan
    # diam-diam membuka tempat bagi kartu sampah lain di bawahnya — terukur
    # 18/09: 61 "paket" baru muncul di 17 situs sebelum dijaga.
    banyak = "".join(
        f"<section><h2>Bagian {i}</h2><table><tr><td>x</td>"
        f"<td>${i}.00</td></tr></table></section>" for i in range(30))
    sisa = extract_dom(BeautifulSoup(f"<body>{banyak}</body>", "lxml"))
    check("penolakan tidak membuka kuota bagi kartu di bawahnya",
          len(sisa) == 0, f"-> {[p.name for p in sisa]}")


def test_skrip_cadangan() -> None:
    """Rencana cadangan kalau GitHub Actions berhenti (18/09/2026).

    Ketentuan GitHub melarang, khusus runner GitHub, "any other activity
    unrelated to the production, testing, deployment, or publication of the
    software project associated with the repository". Pengambil harga harian
    ada di wilayah abu-abu. Kalau workflow dihentikan, arsipnya tidak boleh
    ikut berhenti — satu hari yang hilang tidak bisa dibeli kembali.

    Yang dikunci di sini bukan gaya penulisan skripnya, melainkan tiga syarat
    yang kalau hilang membuat skrip itu berbahaya:
      1. `git pull --rebase` DULU — tanpa itu mesin cadangan tidak tahu
         GitHub Actions sudah mengambil hari ini, dan halaman yang sama
         diambil dua kali sehari (melanggar §9);
      2. arsip di-commit SEBELUM halaman publik dibangun;
      3. tidak pernah memaksa: tidak ada `--force` di mana pun.
    """
    print("\n19. skrip cadangan di luar GitHub Actions")
    import subprocess

    skrip = ROOT / "scripts" / "run_anywhere.sh"
    check("skrip cadangan ada", skrip.exists(), f"-> {skrip}")
    if not skrip.exists():
        return
    isi = skrip.read_text(encoding="utf-8")
    # Pemeriksaan sintaks hanya di mesin POSIX yang punya bash. Di Windows
    # `bash` bisa tidak ada (FileNotFoundError menjatuhkan SELURUH uji —
    # terjadi 21/09/2026 di laptop pemilik) atau berupa peluncur WSL yang
    # tidak mengerti jalur C:\... sehingga menghasilkan GAGAL palsu. Tidak
    # dihitung lolos diam-diam: dicetak jelas sebagai DILEWATI. Workflow
    # test.yml berjalan di ubuntu, jadi di sana pemeriksaan ini tetap wajib.
    bash = shutil.which("bash") if os.name != "nt" else None
    if bash is None:
        print("  DILEWATI pemeriksaan sintaks bash (bash tidak tersedia di "
              "mesin ini; tetap diperiksa di GitHub Actions)")
    else:
        hasil = subprocess.run([bash, "-n", str(skrip)], capture_output=True,
                               text=True)
        check("skrip lolos pemeriksaan sintaks bash", hasil.returncode == 0,
              f"-> {hasil.stderr[:200]}")
    i_pull = isi.find("git pull --rebase")
    i_ambil = isi.find("collector.run")
    i_simpan = isi.find("push_snapshot.py")
    i_situs = isi.find("build_site.py")
    check("menarik arsip terbaru SEBELUM mengambil (penjaga sekali-sehari)",
          0 < i_pull < i_ambil, f"-> pull={i_pull} ambil={i_ambil}")
    check("arsip disimpan SEBELUM halaman publik dibangun",
          0 < i_simpan < i_situs, f"-> simpan={i_simpan} situs={i_situs}")
    check("tidak pernah memaksa push", "--force" not in isi)
    check("tidak pernah melewati penjaga sekali-sehari",
          "--force" not in isi and "collector.run --force" not in isi)
    check("bisa diuji tanpa menyentuh situs vendor",
          "APT_TARGETS_FILE" in isi and "APT_SKIP_PUSH" in isi)


def main() -> int:
    print(f"data uji: {config.DATA_DIR}")
    test_hash_stability()
    test_nested_noise()
    test_site_chrome_noise()
    test_extraction()
    test_secret_redaction()
    test_plan_name_sanity()
    test_phantom_free()
    test_jsonld_only_pages()
    test_parser_version_guard()
    test_corrections_log()
    test_model_tables()
    test_model_table_shapes()
    test_price_moves_need_a_moving_number()
    test_free_name_contradiction()
    test_settle_ms()
    test_heading_tail()
    test_target_diagnosis()
    test_one_request_per_page()
    test_change_classification()
    test_diff()
    test_end_to_end()
    test_site_builder()
    test_audit_2026_09_10()
    test_refusal_is_final_for_the_day()
    test_empat_digit_tanpa_koma()
    test_halaman_publik_16_09()
    test_halaman_per_tool_17_09()
    test_feed_dan_penyaring_18_09()
    test_gerbang_tiga_lapis()
    test_kartu_berisi_tabel_18_09()
    test_skrip_cadangan()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} dari {CHECKS} pemeriksaan GAGAL:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"semua {CHECKS} pemeriksaan lulus")
    return 0


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
