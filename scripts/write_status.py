#!/usr/bin/env python3
"""Tulis docs/status.json: ringkasan snapshot terakhir, untuk pemantau luar.

Dijalankan di antara "Commit snapshot" dan "Bangun & terbitkan halaman
publik" di collect.yml. Berkas ini ikut terbawa `git add -A docs/` milik
build_site.py, jadi tidak perlu commit sendiri.

Prinsip yang dipegang berkas ini — sama dengan build_site.py:

  * TIDAK PERNAH mengambil apa pun dari internet.
  * TIDAK PERNAH menyentuh data/. Hanya membaca riwayat git.
  * Keluarannya DETERMINISTIK: tidak ada jam dinding di dalamnya. Dua kali
    jalan pada arsip yang sama menghasilkan berkas yang sama persis, jadi
    `git diff` tetap bermakna dan tidak ada commit palsu tiap hari.

Sumbernya cuma satu: pesan commit snapshot terakhir, yang formatnya stabil:

    data: snapshot YYYY-MM-DD — N/M ok, X perubahan harga, Y halaman berubah

Sengaja TIDAK membaca data/ — kalau layout arsip berubah, berkas ini tetap
benar.

Pakai:
    python scripts/write_status.py --out docs/status.json

Keluar dengan kode 1 kalau tidak ada commit snapshot, atau pesan commit
terakhir tidak cocok formatnya. Lebih baik gagal berisik di CI daripada
menerbitkan status yang diam-diam salah. Langkahnya di collect.yml memakai
continue-on-error, jadi kegagalan di sini tidak pernah menjatuhkan arsip.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

# "data: snapshot 2026-11-17 — 140/143 ok, 5 perubahan harga, 9 halaman berubah"
# Em dash (—), en dash (–) dan hyphen (-) sama-sama diterima; spasi longgar.
SUBJECT_RE = re.compile(
    r"^data:\s*snapshot\s+(?P<date>\d{4}-\d{2}-\d{2})\s*[—–-]\s*"
    r"(?P<ok>\d+)\s*/\s*(?P<total>\d+)\s*ok\s*,\s*"
    r"(?P<price_changes>\d+)\s*perubahan\s+harga\s*,\s*"
    r"(?P<pages_changed>\d+)\s*halaman\s+berubah\s*$"
)

SCHEMA_VERSION = 1

ROOT = pathlib.Path(__file__).resolve().parent.parent


def git(*args: str) -> str:
    result = subprocess.run(
        ("git", *args), cwd=ROOT, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def latest_snapshot_commit() -> tuple[str, str, str]:
    """(sha, committed_at_iso, subject) dari commit snapshot terbaru."""
    raw = git(
        "log",
        "--grep=^data: snapshot",
        "--extended-regexp",
        "-1",
        "--format=%H%x00%cI%x00%s",
    )
    if not raw:
        sys.exit("FATAL: tidak ada commit dengan pesan diawali 'data: snapshot'.")
    sha, committed_at, subject = raw.split("\x00", 2)
    return sha, committed_at, subject


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="path berkas yang ditulis")
    args = ap.parse_args()

    sha, committed_at, subject = latest_snapshot_commit()

    m = SUBJECT_RE.match(subject)
    if not m:
        sys.exit(
            "FATAL: pesan commit snapshot tidak cocok format yang diharapkan.\n"
            f"  dapat: {subject!r}\n"
            "  pola : data: snapshot YYYY-MM-DD — N/M ok, X perubahan harga, "
            "Y halaman berubah\n"
            "Kalau format pesan di summarize_run.py memang sengaja diubah, "
            "perbarui SUBJECT_RE di berkas ini."
        )

    f = m.groupdict()
    status = {
        "schema_version": SCHEMA_VERSION,
        # Catatan untuk pembaca (termasuk pemantau otomatis): TIDAK ADA stempel
        # "dibuat pada" di berkas ini, dan itu disengaja. Yang menandakan
        # koleksi berhenti adalah last_snapshot.date yang tertinggal dari hari
        # ini — hitung sendiri (hari ini - date). Stempel jam dinding justru
        # akan membuat berkas ini berubah tiap kali dibangun walau isinya sama.
        "_note": (
            "Umur arsip = (tanggal hari ini UTC) - last_snapshot.date. "
            "Berkas ini sengaja tidak memuat jam dinding."
        ),
        "last_snapshot": {
            "date": f["date"],
            "commit_sha": sha,
            "committed_at": committed_at,
            "targets_ok": int(f["ok"]),
            "targets_total": int(f["total"]),
            "price_changes": int(f["price_changes"]),
            "pages_changed": int(f["pages_changed"]),
            "commit_subject": subject,
        },
    }

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"status.json: snapshot {f['date']}, commit {sha[:8]}")


if __name__ == "__main__":
    main()
