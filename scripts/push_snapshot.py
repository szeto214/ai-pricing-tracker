"""Commit + push snapshot harian, tahan terhadap push protection GitHub.

Latar belakang: kita mengarsipkan teks halaman harga publik. Sebagian halaman
memuat CONTOH kredensial (connection string, potongan kode berisi API key).
Push protection GitHub tidak bisa membedakan contoh dari rahasia sungguhan,
dan menolak SELURUH push. Tanpa penanganan, satu string di satu halaman
menghapus arsip 67 halaman lainnya untuk hari itu — dan hari yang hilang tidak
bisa dikejar.

Skrip ini: kalau push ditolak karena push protection, ia membaca berkas dan
baris yang disebut GitHub, menyamarkan baris itu, memperbaiki commit, lalu
mencoba lagi. Yang hilang paling banyak satu baris, bukan satu hari.

    python scripts/push_snapshot.py "pesan commit"
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_ATTEMPTS = 4
BACKOFF = 10

BLOCKED_RE = re.compile(r"GH013|push protection|cannot contain secrets", re.I)
LOCATION_RE = re.compile(r"path:\s*(\S+?):(\d+)")
REDACTED = "<redacted: baris ini ditolak push protection GitHub>"


def run(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=ROOT, check=check, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def branch() -> str:
    name = os.environ.get("GITHUB_REF_NAME")
    if name:
        return name
    out = run("git", "rev-parse", "--abbrev-ref", "HEAD")
    return out.stdout.strip() or "main"


def redact_locations(output: str) -> list[str]:
    """Samarkan baris yang disebut GitHub. Kembalikan daftar yang disamarkan."""
    touched: list[str] = []
    seen: set[tuple[str, int]] = set()
    for rel_path, lineno_s in LOCATION_RE.findall(output):
        lineno = int(lineno_s)
        if (rel_path, lineno) in seen:
            continue
        seen.add((rel_path, lineno))
        # Hanya berkas arsip yang boleh disamarkan otomatis. Kalau yang
        # ditolak adalah kode atau konfigurasi, itu masalah manusia — jangan
        # pernah menyunting kode sendiri lalu mendorongnya diam-diam.
        if not rel_path.replace("\\", "/").startswith("data/"):
            print(f"  ! {rel_path}:{lineno} bukan berkas arsip — "
                  f"tidak disamarkan otomatis, perlu ditangani manual")
            continue
        path = ROOT / rel_path
        if not path.exists():
            print(f"  ! {rel_path} disebut GitHub tapi tidak ada di disk")
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        if not (1 <= lineno <= len(lines)):
            print(f"  ! {rel_path}:{lineno} di luar jangkauan berkas")
            continue
        lines[lineno - 1] = REDACTED
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        touched.append(f"{rel_path}:{lineno}")
    return touched


def main() -> int:
    message = sys.argv[1] if len(sys.argv) > 1 else "data: snapshot"
    ref = branch()

    run("git", "config", "user.name", "pricing-bot")
    run("git", "config", "user.email",
        "41898282+github-actions[bot]@users.noreply.github.com")

    run("git", "add", "-A", "data/")
    if run("git", "diff", "--cached", "--quiet").returncode == 0:
        print("tidak ada perubahan berkas — tidak ada commit")
        return 0

    commit = run("git", "commit", "-m", message)
    print(commit.stdout.strip()[:400])
    if commit.returncode != 0:
        print("commit gagal", file=sys.stderr)
        return 1

    redacted_total: list[str] = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        run("git", "pull", "--rebase", "--autostash", "origin", ref)
        push = run("git", "push", "origin", f"HEAD:{ref}")
        if push.returncode == 0:
            print(push.stdout.strip()[-600:])
            if redacted_total:
                print("\nbaris yang disamarkan supaya push diterima:")
                for loc in redacted_total:
                    print(f"  - {loc}")
            return 0

        print(f"\npush gagal (percobaan {attempt}):")
        print(push.stdout.strip()[-2000:])

        if BLOCKED_RE.search(push.stdout):
            touched = redact_locations(push.stdout)
            if not touched:
                print("push protection menolak tapi lokasinya tidak terbaca — "
                      "perlu ditangani manual", file=sys.stderr)
                return 1
            redacted_total.extend(touched)
            run("git", "add", "-A", "data/")
            amend = run("git", "commit", "--amend", "--no-edit")
            print(amend.stdout.strip()[:300])
            continue

        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF * attempt)

    print("push tetap gagal setelah semua percobaan", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
