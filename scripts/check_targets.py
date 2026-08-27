"""Validator daftar target — jalankan sebelum menambah target ke produksi.

    python -m scripts.check_targets
    python -m scripts.check_targets --only cursor,vercel

Yang diperiksa untuk tiap target:
  * robots.txt mengizinkan atau tidak
  * halaman benar-benar bisa diambil (status HTTP, ukuran)
  * berapa banyak paket & tabel yang berhasil diekstrak
  * apakah halaman butuh render JS (harga tidak ada di HTML awal)

Keluaran: tabel ringkas + rekomendasi. TIDAK menulis apa pun ke data/.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from collector import config, extract, fetcher, normalize  # noqa: E402


async def check(target, *, client, robots, gate, sem) -> dict:
    async with sem:
        res = await fetcher.fetch(target.url, client=client, robots=robots,
                                  gate=gate, render=target.render)
    row = {
        "slug": target.slug,
        "status": res.status,
        "http": res.http_status,
        "plans": 0,
        "tables": 0,
        "extractor": "-",
        "text_kb": 0,
        "note": res.reason[:80],
    }
    if not res.ok:
        return row

    proc = normalize.process(res.html)
    st = extract.extract(target.slug, proc["soup"], res.html)
    row.update({
        "plans": len(st["plans"]),
        "tables": len(st["tables"]),
        "extractor": st["extractor"],
        "text_kb": round(proc["text_bytes"] / 1024, 1),
        "note": "",
    })
    if row["plans"] == 0 and row["tables"] == 0:
        row["note"] = "tidak ada harga di HTML awal -> coba render: js"
    elif row["plans"] == 0:
        row["note"] = "hanya tabel (wajar untuk halaman harga API)"
    elif row["plans"] == 1:
        row["note"] = "cuma 1 paket terdeteksi -> periksa manual"
    return row


async def main_async(args) -> int:
    targets = config.load_targets()
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        targets = [t for t in targets if t.slug in wanted]
    elif not args.include_disabled:
        # Target yang sudah sengaja dinonaktifkan tidak perlu dilaporkan
        # sebagai "bermasalah" tiap kali validasi jalan — itu membuat angka
        # ringkasannya menyesatkan.
        skipped = [t.slug for t in targets if not t.enabled]
        targets = [t for t in targets if t.enabled]
        if skipped:
            print(f"dilewati ({len(skipped)} nonaktif): {', '.join(skipped)}\n")
    if args.limit:
        targets = targets[: args.limit]

    sem = asyncio.Semaphore(args.concurrency)
    gate = fetcher.HostGate()
    async with fetcher.make_client() as client:
        robots = fetcher.RobotsCache(client, gate)
        rows = await asyncio.gather(*[
            check(t, client=client, robots=robots, gate=gate, sem=sem)
            for t in targets
        ], return_exceptions=True)

    ok = bad = 0
    print(f"{'slug':<20} {'status':<14} {'http':<5} {'plan':<5} {'tbl':<4} "
          f"{'ekstraktor':<12} {'kb':<6} catatan")
    print("-" * 108)
    for t, row in zip(targets, rows):
        if isinstance(row, BaseException):
            # Sebutkan berkas:baris terakhir — tanpa itu, crash butuh satu
            # putaran validasi lagi hanya untuk tahu di mana letaknya.
            where = ""
            tb = row.__traceback__
            while tb is not None:
                where = f"{tb.tb_frame.f_code.co_filename.split('/')[-1]}:{tb.tb_lineno}"
                tb = tb.tb_next
            print(f"{t.slug:<20} crash          -     -     -    -            -      "
                  f"{type(row).__name__}: {row} @ {where}")
            bad += 1
            continue
        if row["status"] == "ok":
            ok += 1
        else:
            bad += 1
        print(f"{row['slug']:<20} {row['status']:<14} {str(row['http'] or '-'):<5} "
              f"{row['plans']:<5} {row['tables']:<4} {row['extractor']:<12} "
              f"{row['text_kb']:<6} {row['note']}")

    print("-" * 108)
    print(f"{ok} bisa diambil, {bad} bermasalah, dari {len(targets)} target")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--only")
    p.add_argument("--limit", type=int)
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--include-disabled", action="store_true",
                   help="ikut menguji target enabled: false (untuk mengecek "
                        "apakah blokirnya sudah dicabut)")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
