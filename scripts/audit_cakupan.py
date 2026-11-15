#!/usr/bin/env python3
"""Audit cakupan: apakah ada sumber data yang diam-diam menyusut?

Kenapa alat ini ada
-------------------
Status `134/134 ok` hanya berarti HTTP-nya berhasil. Dua kali dalam sebulan
sumber data lenyap sambil tetap melaporkan ok:

  23/10  fal.ai & fireworks-ai membuang `<table>` -> 106 baris harga hilang
  03/11  baseten & gemini berubah jadi cangkang SPA -> 39 baris hilang

Keduanya ditemukan bukan dari status, melainkan dari membandingkan JUMLAH
BARIS yang terbaca per target antar hari. Pemeriksaan itu terlalu penting
untuk diketik ulang dari ingatan tiap kali, jadi ia ditaruh di sini.

Alat ini HANYA MEMBACA `data/runs/*.json`. Tidak menyentuh jaringan, tidak
mengambil satu pun halaman vendor, tidak menulis apa pun.

    python scripts/audit_cakupan.py            # 7 hari vs 7 hari sebelumnya
    python scripts/audit_cakupan.py --hari 14  # jendela 14 hari

Keluar dengan kode 1 kalau ada target yang menyusut >50% atau jatuh ke nol —
supaya bisa dipakai sebagai penjaga, bukan sekadar laporan.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collector import config  # noqa: E402

RUNS = config.RUNS_DIR


def baris_terbaca(entry: dict) -> int:
    """Berapa baris harga yang benar-benar terbaca dari satu halaman."""
    return (entry.get("plans_count") or 0) + (entry.get("models_count") or 0)


def muat(hari: list[str]) -> dict[str, int]:
    """Puncak jumlah baris per slug sepanjang rentang hari itu.

    Dipakai PUNCAK, bukan rata-rata: satu hari gagal ambil tidak boleh
    terlihat seperti sumber yang menyusut.
    """
    out: dict[str, int] = {}
    for h in hari:
        p = RUNS / f"{h}.json"
        if not p.exists():
            continue
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for t in r.get("targets") or []:
            s = t.get("slug")
            if s:
                out[s] = max(out.get(s, 0), baris_terbaca(t))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hari", type=int, default=7,
                    help="panjang jendela pembanding (bawaan 7)")
    a = ap.parse_args()
    return main_untuk_uji(hari=a.hari)


def main_untuk_uji(hari: int = 7) -> int:
    """Isi `main()` tanpa argparse, supaya bisa diuji tanpa menyentuh argv."""
    n = max(1, hari)

    hari = sorted(p.stem for p in RUNS.glob("*.json"))
    if len(hari) < n + 1:
        print(f"arsip baru {len(hari)} hari — belum cukup untuk jendela {n} hari")
        return 0

    aktif = {t.slug for t in config.load_targets() if t.enabled}
    lama = muat(hari[-2 * n:-n])
    baru = muat(hari[-n:])

    menyusut, lenyap, tumbuh = [], [], []
    for s in sorted(aktif):
        a_, b_ = lama.get(s, 0), baru.get(s, 0)
        if a_ > 0 and b_ == 0:
            lenyap.append((s, a_, b_))
        elif a_ > 0 and b_ * 2 < a_:
            menyusut.append((s, a_, b_))
        elif b_ > max(a_ * 2, 1):
            tumbuh.append((s, a_, b_))

    nol = sorted(s for s in aktif if baru.get(s, 0) == 0 and lama.get(s, 0) == 0)

    print(f"Audit cakupan — {hari[-n]} s/d {hari[-1]} "
          f"dibanding {hari[-2 * n]} s/d {hari[-n - 1]}")
    print(f"target aktif: {len(aktif)}")
    print()
    for judul, daftar in (("LENYAP (ada -> nol)", lenyap),
                          ("MENYUSUT >50%", menyusut),
                          ("tumbuh >2x", tumbuh)):
        print(f"{judul}: {len(daftar)}")
        for s, a_, b_ in daftar:
            print(f"    {s:<22} {a_:>4} -> {b_:>4} baris")
    print(f"nol baris di KEDUA jendela: {len(nol)}")
    if nol:
        print("    " + ", ".join(nol))
        print("    (periksa dulu: adakah angka harga di TEKS arsipnya? "
              "halaman tanpa harga -> nonaktifkan; harga ada tapi tak terbaca "
              "-> perbaiki pembacanya)")

    return 1 if (lenyap or menyusut) else 0


if __name__ == "__main__":
    raise SystemExit(main())
