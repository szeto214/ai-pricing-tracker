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

PEMBACAANNYA SENGAJA LONGGAR. Yang wajib cuma TANGGAL; angka-angka lain
bonus. Alasannya nyata: versi pertama berkas ini (17/11/2026) menuntut
seluruh pesan commit cocok satu pola, lalu menolak menulis apa pun ketika
ternyata pesan aslinya memuat "(10 angka)" dan "4 perubahan katalog" —
dua bagian yang tidak ada di pola. Hasilnya status.json tidak pernah
terbit, dan pemantauan tetap buta.

Pemantau di luar hanya perlu menjawab satu pertanyaan: "arsipnya masih
hidup atau tidak?" Itu dijawab oleh tanggal. Kalau suatu hari format
angkanya berubah lagi, berkas ini tetap terbit dengan tanggal yang benar
dan kehilangan detailnya saja — bukan kehilangan fungsinya.

Pakai:
    python scripts/write_status.py --out docs/status.json

Keluar dengan kode 1 HANYA kalau tidak ada commit snapshot sama sekali,
atau tanggalnya tidak terbaca. Langkahnya di collect.yml memakai
continue-on-error, jadi kegagalan di sini tidak pernah menjatuhkan arsip.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

SCHEMA_VERSION = 1

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Pesan commit snapshot, per 17/11/2026:
#   data: snapshot 2026-11-17 — 132/132 ok, 1 perubahan harga (10 angka),
#   4 perubahan katalog, 22 halaman berubah
#
# Satu-satunya bagian yang WAJIB: tanggalnya.
DATE_RE = re.compile(r"^data:\s*snapshot\s+(\d{4}-\d{2}-\d{2})\b")

# Sisanya dicari sendiri-sendiri, jadi hilangnya satu tidak menjatuhkan yang
# lain. Tiap pola dicocokkan ke mana pun di dalam pesan.
OPSIONAL = {
    "targets_ok": re.compile(r"(\d+)\s*/\s*\d+\s*ok\b"),
    "targets_total": re.compile(r"\d+\s*/\s*(\d+)\s*ok\b"),
    # "1 perubahan harga" = jumlah HALAMAN yang harganya bergerak.
    "price_change_pages": re.compile(r"(\d+)\s*perubahan\s+harga\b"),
    # "(10 angka)" = jumlah ANGKA yang bergerak. Jauh lebih besar dari
    # price_change_pages, dan keduanya memang beda hal.
    "price_moves": re.compile(r"\(\s*(\d+)\s*angka\s*\)"),
    "catalog_changes": re.compile(r"(\d+)\s*perubahan\s+katalog\b"),
    "pages_changed": re.compile(r"(\d+)\s*halaman\s+berubah\b"),
}


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

    m = DATE_RE.match(subject)
    if not m:
        sys.exit(
            "FATAL: tanggal tidak terbaca dari pesan commit snapshot.\n"
            f"  dapat: {subject!r}\n"
            "  pola : diawali 'data: snapshot YYYY-MM-DD'\n"
            "Hanya tanggalnya yang wajib; sisanya opsional."
        )

    snapshot = {
        "date": m.group(1),
        "commit_sha": sha,
        "committed_at": committed_at,
        # Pesan mentahnya ikut disimpan, supaya apa pun yang gagal diurai di
        # atas tetap bisa dibaca manusia tanpa membuka repo.
        "commit_subject": subject,
    }

    hilang = []
    for nama, pola in OPSIONAL.items():
        cocok = pola.search(subject)
        # null, bukan dihilangkan: bentuk berkasnya tetap sama tiap hari
        # sehingga pembacanya tidak perlu menebak field mana yang ada.
        snapshot[nama] = int(cocok.group(1)) if cocok else None
        if not cocok:
            hilang.append(nama)

    status = {
        "schema_version": SCHEMA_VERSION,
        # Tidak ada stempel "dibuat pada" di berkas ini, dan itu disengaja:
        # stempel jam dinding membuat berkas berubah tiap kali dibangun walau
        # isinya sama, lalu memaksa commit palsu tiap hari. Yang menandakan
        # koleksi berhenti adalah date yang tertinggal dari hari ini.
        "_note": (
            "Umur arsip = (tanggal hari ini UTC) - last_snapshot.date. "
            "Berkas ini sengaja tidak memuat jam dinding. Field bernilai "
            "null berarti bagian itu tidak ada di pesan commit, bukan nol."
        ),
        "last_snapshot": snapshot,
    }

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"status.json: snapshot {snapshot['date']}, commit {sha[:8]}")
    if hilang:
        print("  (tidak terbaca dari pesan commit, ditulis null: "
              + ", ".join(hilang) + ")")


if __name__ == "__main__":
    main()
