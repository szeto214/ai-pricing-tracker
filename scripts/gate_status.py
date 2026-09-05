"""Hitung gerbang bulan ke-3 dengan satu definisi yang tetap.

    python scripts/gate_status.py
    python scripts/gate_status.py --markdown

Kenapa ini ada. Angka gerbang tidak boleh dihitung ulang dengan cara berbeda
tiap kali ditanyakan — kalau begitu, gerbangnya kehilangan arti. Skrip ini
satu-satunya sumber angka itu, dan aturannya ditulis di sini supaya bisa
diperiksa siapa pun:

  * Yang dihitung adalah HALAMAN-HARI, bukan jumlah angka. Satu halaman API
    bisa menggerakkan 40 angka sekaligus; kalau dihitung per angka, satu
    halaman ramai bisa memenuhi gerbang sendirian dan itu bukan bukti apa pun.
  * Yang menentukan BUKAN label `kind` yang tersimpan, melainkan isinya:
    sebuah halaman-hari dihitung hanya kalau benar-benar ada peristiwa
    `price_changed` atau `model_price_changed` di dalamnya. Sampai 03/09/2026
    label `price_change` masih diberikan juga untuk paket yang muncul/hilang;
    dari 38 catatan berlabel itu, 18 di antaranya tidak memuat satu pun angka
    yang bergerak. Kalau label lama ikut dihitung, angka gerbang menggelembung
    hampir dua kali lipat. Label lamanya tidak diubah — arsip tidak ditulis
    ulang — tapi penghitungnya memakai satu definisi untuk seluruh rentang.
  * Sewa GPU dihitung TERPISAH. Harganya bergerak tiap hari mengikuti pasar
    spot; mencampurnya akan menutupi pertanyaan sesungguhnya, yaitu apakah
    harga SOFTWARE cukup sering berubah untuk membuat arsip ini berguna.
  * Peristiwa yang tercatat di corrections.jsonl dikeluarkan. Log aslinya
    tidak pernah diubah.

Tidak menulis apa pun.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import config, storage  # noqa: E402

GATE_TARGET = 100          # gerbang bulan ke-3
GPU_CATEGORY = "gpu-rental"


def load_changes() -> list[dict]:
    if not config.CHANGES_LOG.exists():
        return []
    out = []
    for line in config.CHANGES_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--markdown", action="store_true")
    args = p.parse_args()

    rows = load_changes()
    if not rows:
        print("Belum ada catatan perubahan.")
        return 0

    corrections = storage.load_corrections()
    gpu_slugs = {t.slug for t in config.load_targets()
                 if t.category == GPU_CATEGORY}

    def moved_in(c: dict) -> int:
        return (len([e for e in c.get("plan_events") or []
                     if e["type"] == "price_changed"])
                + sum(len(e["changes"]) for e in c.get("model_events") or []
                      if e["type"] == "model_price_changed"))

    # Bukti, bukan label. Lihat penjelasan di docstring.
    price = [c for c in rows if moved_in(c) > 0]
    berlabel = len([c for c in rows if c.get("kind") == "price_change"])
    kept = [c for c in price
            if (c.get("date"), c.get("slug"), "price_change") not in corrections]
    dropped = len(price) - len(kept)

    software = [c for c in kept if c.get("slug") not in gpu_slugs]
    gpu = [c for c in kept if c.get("slug") in gpu_slugs]

    dates = sorted({c["date"] for c in rows if c.get("date")})
    first, last = dates[0], dates[-1]
    days = (dt.date.fromisoformat(last) - dt.date.fromisoformat(first)).days + 1
    rate = len(software) / days if days else 0.0
    proyeksi = round(rate * 90)

    def numbers(items):
        return sum(moved_in(c) for c in items)

    lines = [
        "## Status gerbang bulan ke-3",
        "",
        f"Rentang data: **{first} s/d {last}** ({days} hari)",
        "",
        f"- Perubahan harga SOFTWARE: **{len(software)} / {GATE_TARGET}** "
        f"halaman-hari  ({numbers(software)} angka)",
        f"- Perubahan harga sewa GPU (dihitung terpisah): **{len(gpu)}** "
        f"halaman-hari  ({numbers(gpu)} angka)",
        f"- Dikeluarkan oleh koreksi: **{dropped}** halaman-hari",
        f"- Berlabel `price_change` di arsip: {berlabel} — selisihnya adalah "
        f"catatan sebelum 03/09 yang isinya hanya paket muncul/hilang",
        "",
        f"Laju software: **{rate:.2f} / hari** -> proyeksi 90 hari: "
        f"**~{proyeksi}**",
    ]
    if proyeksi < GATE_TARGET:
        lines += [
            "",
            f"> Dengan laju sekarang gerbang **tidak tercapai** "
            f"(~{proyeksi} dari {GATE_TARGET}). Yang menaikkan angka ini bukan "
            f"penambahan target, melainkan ekstraksi yang membaca lebih banyak "
            f"halaman dengan benar — halaman yang harganya memang sering "
            f"berubah sudah ada di daftar, hanya belum terbaca.",
        ]

    top = Counter(c["name"] for c in software).most_common(8)
    if top:
        lines += ["", "### Penyumbang terbanyak (software)", "",
                  "| tool | halaman-hari |", "| --- | --- |"]
        lines += [f"| {name} | {n} |" for name, n in top]

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
