"""Ringkas log eksekusi terakhir.

    python scripts/summarize_run.py            -> baris untuk $GITHUB_OUTPUT
    python scripts/summarize_run.py --markdown -> ringkasan untuk halaman run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import config, storage  # noqa: E402


def latest_run() -> dict | None:
    files = sorted(config.RUNS_DIR.glob("*.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def changes_for(date: str) -> list[dict]:
    if not config.CHANGES_LOG.exists():
        return []
    out = []
    for line in config.CHANGES_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("date") == date:
            out.append(entry)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--markdown", action="store_true")
    args = p.parse_args()

    run = latest_run()
    if not run:
        if args.markdown:
            print("Belum ada log eksekusi.")
        else:
            print("message=data: tidak ada log eksekusi")
        return 0

    s = run["summary"]
    date = run["date"]
    changes = changes_for(date)
    # `price_change` = angka bergerak pada paket yang ada di kedua hari.
    # `catalog_change` = paket muncul/hilang — berguna, tapi bergantung pada
    # kestabilan ekstraksi, jadi dihitung terpisah dan TIDAK dipakai untuk
    # gerbang bulan ke-3.
    corrections = storage.load_corrections()
    price_changes = [c for c in changes if c["kind"] == "price_change"
                     and (date, c["slug"], "price_change") not in corrections]
    corrected = [c for c in changes if c["kind"] == "price_change"
                 and (date, c["slug"], "price_change") in corrections]
    catalog_changes = [c for c in changes if c["kind"] == "catalog_change"]
    reparsed = [c for c in changes if c["kind"] == "parser_upgrade"]

    # Halaman-hari vs angka: satu halaman API bisa menggerakkan puluhan harga
    # model sekaligus. Gerbang bulan ke-3 tetap dihitung per halaman-hari
    # supaya satu halaman ramai tidak bisa memenuhinya sendirian.
    moved_numbers = sum(
        len([e for e in c["plan_events"] if e["type"] == "price_changed"])
        + sum(len(e["changes"]) for e in c.get("model_events") or []
              if e["type"] == "model_price_changed")
        for c in price_changes
    )

    if not args.markdown:
        msg = (f"data: snapshot {date} — {s['ok']}/{s['total']} ok, "
               f"{len(price_changes)} perubahan harga ({moved_numbers} angka), "
               f"{len(catalog_changes)} perubahan katalog, "
               f"{len(changes)} halaman berubah")
        print(f"message={msg}")
        print(f"ok={s['ok']}")
        print(f"changed={len(changes)}")
        print(f"price_changes={len(price_changes)}")
        print(f"moved_numbers={moved_numbers}")
        return 0

    print(f"## Snapshot {date}\n")
    print(f"- Target berhasil: **{s['ok']}/{s['total']}**")
    print(f"- Halaman berubah: **{len(changes)}**")
    print(f"- Perubahan harga (angka bergerak): **{len(price_changes)}** halaman, "
          f"{moved_numbers} angka")
    print(f"- Perubahan katalog (paket muncul/hilang): "
          f"**{len(catalog_changes)}**")
    if corrected:
        print(f"- Dikoreksi (terbukti cacat pembaca, tidak dihitung): "
              f"**{len(corrected)}** — {', '.join(c['slug'] for c in corrected)}")
    if reparsed:
        print(f"- Pembaca angka baru saja dinaikkan versinya: "
              f"**{len(reparsed)}** halaman tidak dihitung hari ini")
    print()

    failed = [t for t in run["targets"] if t.get("status") != "ok"]
    if failed:
        print("### Target bermasalah\n")
        print("| slug | status | keterangan |")
        print("| --- | --- | --- |")
        for t in failed:
            print(f"| {t['slug']} | {t['status']} | "
                  f"{(t.get('reason') or '')[:90]} |")
        print()

    if catalog_changes:
        print("### Perubahan katalog\n")
        for c in catalog_changes:
            evs = [e for e in c["plan_events"]
                   if e["type"] in ("plan_added", "plan_removed")]
            head = [f"{e['type'].replace('plan_', '')}: {e.get('plan', '')}"
                    for e in evs[:4]]
            mevs = [e for e in c.get("model_events") or []
                    if e["type"] in ("model_added", "model_removed")]
            head += [f"{e['type'].replace('model_', 'model ')}: {e.get('model', '')}"
                     for e in mevs[:4]]
            print(f"- **{c['name']}** — {', '.join(head)}")
        print()

    if price_changes:
        rows = [(c["name"], ev.get("plan", ""), ev["from"].get("raw", ""),
                 ev["to"].get("raw", ""), ev.get("pct_change", ""))
                for c in price_changes for ev in c["plan_events"]
                if ev["type"] == "price_changed"]
        if rows:
            print("### Perubahan harga paket\n")
            print("| tool | paket | dari | ke | % |")
            print("| --- | --- | --- | --- | --- |")
            for r in rows:
                print("| " + " | ".join(str(x) for x in r) + " |")
            print()

        # Harga per-model dilaporkan terpisah: satu halaman API bisa
        # menggerakkan puluhan angka sekaligus, dan mencampurnya ke tabel di
        # atas akan menenggelamkan perubahan paket yang jumlahnya sedikit.
        mrows = [(c["name"], ev.get("model", ""), ch["field"],
                  ch["from"].get("raw", ""), ch["to"].get("raw", ""),
                  ch.get("pct_change", ""), ev.get("unit", ""))
                 for c in price_changes for ev in c.get("model_events") or []
                 if ev["type"] == "model_price_changed" for ch in ev["changes"]]
        if mrows:
            print(f"### Perubahan harga model ({len(mrows)} angka)\n")
            print("| tool | model | kolom | dari | ke | % | satuan |")
            print("| --- | --- | --- | --- | --- | --- | --- |")
            for r in mrows[:60]:
                print("| " + " | ".join(str(x) for x in r) + " |")
            if len(mrows) > 60:
                print(f"\n_{len(mrows) - 60} baris lain ada di changes.jsonl._")
            print()

    low = [t for t in run["targets"]
           if t.get("status") == "ok" and t.get("confidence") in ("low", "none")]
    if low:
        print(f"### Ekstraksi lemah ({len(low)}) — kandidat adapter\n")
        print(", ".join(t["slug"] for t in low))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
