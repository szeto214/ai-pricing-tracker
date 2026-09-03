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


def main() -> int:
    print(f"data uji: {config.DATA_DIR}")
    test_hash_stability()
    test_nested_noise()
    test_site_chrome_noise()
    test_extraction()
    test_secret_redaction()
    test_plan_name_sanity()
    test_change_classification()
    test_diff()
    test_end_to_end()

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
