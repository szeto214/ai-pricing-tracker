"""Titik masuk pengumpul data.

    python -m collector.run                     # jalan penuh (sekali per hari)
    python -m collector.run --only cursor,vercel
    python -m collector.run --limit 5 --dry-run
    python -m collector.run --force             # abaikan penjaga sekali-sehari
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

from . import config, extract, fetcher, normalize, storage
from .diff import compare


def today_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


async def process_target(target, *, client, robots, gate, sem, date, args) -> dict:
    entry = {
        "slug": target.slug,
        "name": target.name,
        "url": target.url,
        "status": "pending",
    }
    async with sem:
        res = await fetcher.fetch(
            target.url, client=client, robots=robots, gate=gate,
            render=target.render,
        )

    entry["http_status"] = res.http_status
    entry["elapsed_ms"] = res.elapsed_ms
    entry["robots_note"] = res.robots_note

    if not res.ok:
        entry["status"] = res.status
        entry["reason"] = res.reason
        return entry

    try:
        proc = normalize.process(res.html)
    except Exception as exc:  # noqa: BLE001
        entry["status"] = "parse_error"
        entry["reason"] = f"{type(exc).__name__}: {exc}"
        return entry

    structured = extract.extract(target.slug, proc["soup"], res.html)

    record = {
        "slug": target.slug,
        "name": target.name,
        "vendor": target.vendor,
        "category": target.category,
        "url": target.url,
        "final_url": res.final_url or target.url,
        "parser_version": config.PARSER_VERSION,
        "content_hash": proc["content_hash"],
        "raw_hash": proc["raw_hash"],
        "text_bytes": proc["text_bytes"],
        "extractor": structured["extractor"],
        "confidence": structured["confidence"],
        "plans": structured["plans"],
        # Tanpa baris ini, harga per-model diekstrak setiap hari lalu dibuang
        # sebelum disimpan. Rekaman kemarin tidak pernah punya kunci "models",
        # penjaga dasar-belum-ada selalu aktif, dan `diff_models` selalu
        # mengembalikan daftar kosong. Fiturnya tampak jalan — models_count di
        # log eksekusi menunjukkan 34 untuk deepinfra — padahal mustahil
        # mendeteksi apa pun. Diam-diam mati selama dua hari.
        "models": structured.get("models") or [],
        "tables": structured["tables"],
        "extract_errors": structured["extract_errors"],
    }

    old = storage.load_current(target.slug)
    change = compare(old, record, proc["text"])

    entry.update({
        "status": "ok",
        "content_hash": proc["content_hash"],
        "extractor": structured["extractor"],
        "confidence": structured["confidence"],
        "plans_count": len(structured["plans"]),
        "models_count": len(structured.get("models") or []),
        "tables_count": len(structured["tables"]),
        # Diambil dengan sukses, tapi isinya nyaris tidak ada — cangkang SPA
        # kosong. Statusnya SENGAJA tetap "ok": penjaga sekali-per-hari memakai
        # status itu, dan menandainya gagal akan membuat halaman yang sama
        # diambil dua kali sehari. Ini penanda terpisah, untuk dilaporkan.
        "thin": proc["text_bytes"] < config.THIN_TEXT_BYTES,
        "changed": change is not None,
        "change_kind": change["kind"] if change else None,
    })
    if structured["extract_errors"]:
        entry["extract_errors"] = structured["extract_errors"]

    if args.dry_run:
        entry["dry_run"] = True
        return entry

    storage.save_current(record, proc["text"])

    if change is not None:
        if not args.no_raw:
            storage.save_raw_snapshot(target.slug, date, proc["archive_html"])
        entry["_change_entry"] = {
            "date": date,
            "observed_at": now_iso(),
            "slug": target.slug,
            "name": target.name,
            "vendor": target.vendor,
            "category": target.category,
            "url": target.url,
            "kind": change["kind"],
            "content_hash_before": (old or {}).get("content_hash"),
            "content_hash_after": record["content_hash"],
            "extractor": record["extractor"],
            "confidence": record["confidence"],
            "plan_events": change["plan_events"],
            "model_events": change.get("model_events") or [],
            "text_diff": change["text"],
        }
    return entry


def dedupe_by_url(targets: list) -> tuple[list, list[tuple[str, str]]]:
    """Satu halaman = satu permintaan per eksekusi.

    Aturan kita sendiri: "Ambil maksimal sekali sehari per halaman." Penjaga
    sekali-per-hari di bawah memakai kunci slug, jadi dua slug berbeda yang
    menunjuk halaman yang sama lolos begitu saja. Yang pertama muncul di
    targets.yaml dipertahankan; sisanya dilewati dan dilaporkan.
    """
    seen: dict[str, str] = {}
    kept, dropped = [], []
    for t in targets:
        key = t.fetch_key
        if key in seen:
            dropped.append((seen[key], t.slug))
            continue
        seen[key] = t.slug
        kept.append(t)
    return kept, dropped


async def main_async(args) -> int:
    config.ensure_dirs()
    targets = config.load_targets(
        __import__("pathlib").Path(args.targets) if args.targets else None
    )

    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        unknown = wanted - {t.slug for t in targets}
        if unknown:
            print(f"slug tidak dikenal: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        targets = [t for t in targets if t.slug in wanted]

    targets = [t for t in targets if t.enabled]
    targets, duplicates = dedupe_by_url(targets)
    for kept, dropped in duplicates:
        # Jaring pengaman, bukan pengganti kerapian daftar target: kalau ini
        # sampai tercetak, targets.yaml perlu dibereskan. Tapi selama belum,
        # halaman itu tetap hanya diambil sekali.
        print(f"! {dropped} memakai halaman yang sama dengan {kept} "
              f"— dilewati supaya tidak dua kali sehari", file=sys.stderr)
    if args.limit:
        targets = targets[: args.limit]

    date = args.date or today_utc()
    previous = storage.load_run_log(date) or {}
    done_ok = set()
    if not args.force:
        done_ok = {
            e["slug"] for e in previous.get("targets", [])
            if e.get("status") == "ok" and not e.get("dry_run")
        }
        if done_ok:
            targets = [t for t in targets if t.slug not in done_ok]

    if not targets:
        print(f"[{date}] semua target sudah diambil hari ini. "
              f"Pakai --force untuk mengulang.")
        return 0

    print(f"[{date}] mengambil {len(targets)} target "
          f"(konkurensi {args.concurrency}, UA: {config.USER_AGENT})")

    sem = asyncio.Semaphore(args.concurrency)
    gate = fetcher.HostGate()

    async with fetcher.make_client() as client:
        robots = fetcher.RobotsCache(client, gate)
        results = await asyncio.gather(*[
            process_target(t, client=client, robots=robots, gate=gate,
                           sem=sem, date=date, args=args)
            for t in targets
        ], return_exceptions=True)

    entries: list[dict] = []
    changes: list[dict] = []
    for target, res in zip(targets, results):
        if isinstance(res, BaseException):
            entries.append({
                "slug": target.slug, "name": target.name, "url": target.url,
                "status": "crash", "reason": f"{type(res).__name__}: {res}",
            })
            continue
        change_entry = res.pop("_change_entry", None)
        if change_entry:
            changes.append(change_entry)
        entries.append(res)

    if not args.dry_run:
        storage.append_changes(changes)
        merged = {e["slug"]: e for e in previous.get("targets", [])}
        merged.update({e["slug"]: e for e in entries})
        log = {
            "date": date,
            "finished_at": now_iso(),
            "user_agent": config.USER_AGENT,
            "summary": _summarize(list(merged.values())),
            "targets": sorted(merged.values(), key=lambda e: e["slug"]),
        }
        storage.save_run_log(date, log)

    _print_summary(entries, changes)
    return 0


def _summarize(entries: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    for e in entries:
        by_status[e.get("status", "?")] = by_status.get(e.get("status", "?"), 0) + 1
    return {
        "total": len(entries),
        "ok": by_status.get("ok", 0),
        "changed": sum(1 for e in entries if e.get("changed")),
        "by_status": dict(sorted(by_status.items())),
    }


def _print_summary(entries: list[dict], changes: list[dict]) -> None:
    s = _summarize(entries)
    print(f"\nselesai: {s['ok']}/{s['total']} ok, {s['changed']} berubah")
    for status, count in s["by_status"].items():
        if status != "ok":
            print(f"  {status}: {count}")
            for e in entries:
                if e.get("status") == status:
                    print(f"    - {e['slug']}: {e.get('reason', '')[:110]}")
    if changes:
        print("\nperubahan tercatat:")
        for c in changes:
            events = c["plan_events"]
            head = ", ".join(
                f"{ev['type']}:{ev.get('plan', '')}" for ev in events[:3]
            ) or f"{c['text_diff']['lines_added']}+/{c['text_diff']['lines_removed']}-"
            print(f"  - {c['slug']} [{c['kind']}] {head}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pengumpul harga tool AI & software")
    p.add_argument("--targets", help="berkas daftar target (default targets/targets.yaml)")
    p.add_argument("--only", help="daftar slug dipisah koma")
    p.add_argument("--limit", type=int, help="batasi jumlah target")
    p.add_argument("--date", help="tanggal snapshot (YYYY-MM-DD, default hari ini UTC)")
    p.add_argument("--concurrency", type=int, default=config.MAX_CONCURRENCY)
    p.add_argument("--force", action="store_true",
                   help="abaikan penjaga sekali-per-hari")
    p.add_argument("--dry-run", action="store_true", help="jangan tulis apa pun")
    p.add_argument("--no-raw", action="store_true",
                   help="jangan simpan arsip HTML mentah")
    return p


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
