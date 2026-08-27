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

from collector import config  # noqa: E402


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
    price_changes = [c for c in changes if c["kind"] == "price_change"]

    if not args.markdown:
        msg = (f"data: snapshot {date} — {s['ok']}/{s['total']} ok, "
               f"{len(price_changes)} perubahan harga, "
               f"{len(changes)} halaman berubah")
        print(f"message={msg}")
        print(f"ok={s['ok']}")
        print(f"changed={len(changes)}")
        return 0

    print(f"## Snapshot {date}\n")
    print(f"- Target berhasil: **{s['ok']}/{s['total']}**")
    print(f"- Halaman berubah: **{len(changes)}**")
    print(f"- Perubahan harga: **{len(price_changes)}**\n")

    failed = [t for t in run["targets"] if t.get("status") != "ok"]
    if failed:
        print("### Target bermasalah\n")
        print("| slug | status | keterangan |")
        print("| --- | --- | --- |")
        for t in failed:
            print(f"| {t['slug']} | {t['status']} | "
                  f"{(t.get('reason') or '')[:90]} |")
        print()

    if price_changes:
        print("### Perubahan harga\n")
        print("| tool | paket | dari | ke | % |")
        print("| --- | --- | --- | --- | --- |")
        for c in price_changes:
            for ev in c["plan_events"]:
                if ev["type"] != "price_changed":
                    continue
                print(f"| {c['name']} | {ev.get('plan', '')} | "
                      f"{ev['from'].get('raw', '')} | {ev['to'].get('raw', '')} | "
                      f"{ev.get('pct_change', '')} |")
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
