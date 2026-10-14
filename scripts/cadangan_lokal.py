#!/usr/bin/env python3
"""Membuat cadangan SELURUH riwayat arsip ke satu berkas .bundle.

Kenapa ini ada. Arsip ini hidup dari riwayat git yang menumpuk; satu hari
yang hilang tidak bisa dibeli kembali. Sudah ada dua salinan daring (GitHub
dan cermin Codeberg), tetapi keduanya milik pihak lain dan tunduk pada
ketentuan pihak lain — Codeberg bahkan sudah mengubah Syarat Pakainya
(23/07/2026) untuk menolak proyek yang ditulis dengan bantuan LLM berat.
Cadangan yang tidak bisa dicabut siapa pun hanya satu: berkas di tangan
pemilik.

`git bundle` menyimpan seluruh riwayat — tiap commit, tiap berkas, tiap
tanggal — di dalam SATU berkas. Memulihkannya cukup dengan:

    git clone ai-pricing-tracker-2026-10-14.bundle pulih

Hasilnya repo lengkap, tanpa perlu GitHub, tanpa perlu internet.

Cara pakai (dari folder repo, venv aktif):

    python scripts/cadangan_lokal.py

Pilihan:
    --ke <folder>     tempat menaruh berkas (bawaan: folder "cadangan" di
                      SEBELAH repo, bukan di dalamnya — supaya tidak pernah
                      ikut ter-commit)
    --tanpa-ambil     jangan `git fetch` dulu (untuk kerja luring)
    --simpan N        ingatkan kalau berkas cadangan sudah lebih dari N
                      (bawaan 6). Skrip ini TIDAK PERNAH menghapus apa pun.

Skrip ini hanya membaca repo: tidak mengubah berkas kerja, tidak membuat
commit, tidak mendorong apa pun, dan tidak pernah menyentuh kredensial.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

AKAR = Path(__file__).resolve().parent.parent
NAMA = "ai-pricing-tracker"


def jalankan(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Menjalankan perintah git dan mengembalikan hasilnya (tanpa melempar)."""
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )


def wajib(args: list[str], cwd: Path, pesan: str) -> str:
    hasil = jalankan(args, cwd)
    if hasil.returncode != 0:
        print(f"GAGAL: {pesan}")
        print((hasil.stderr or hasil.stdout).strip())
        sys.exit(1)
    return hasil.stdout.strip()


def ukuran_rapi(byte: int) -> str:
    satuan = ["B", "KiB", "MiB", "GiB"]
    nilai = float(byte)
    for s in satuan:
        if nilai < 1024 or s == satuan[-1]:
            return f"{nilai:.1f} {s}"
        nilai /= 1024
    return f"{byte} B"


def main() -> int:
    p = argparse.ArgumentParser(description="Cadangan lokal arsip ke satu berkas .bundle")
    p.add_argument("--ke", default=None, help="folder tujuan")
    p.add_argument("--tanpa-ambil", action="store_true", help="lewati git fetch")
    p.add_argument("--simpan", type=int, default=6, help="ingatkan bila berkas lebih dari N")
    a = p.parse_args()

    if not (AKAR / ".git").exists():
        print(f"GAGAL: {AKAR} bukan repo git.")
        return 1

    tujuan = Path(a.ke).expanduser().resolve() if a.ke else (AKAR.parent / "cadangan")
    # Diperiksa SEBELUM folder dibuat: menolak tempat yang salah tidak boleh
    # meninggalkan jejak folder kosong di dalam repo.
    if tujuan == AKAR or AKAR in tujuan.parents:
        print("GAGAL: folder tujuan ada DI DALAM repo. Cadangan harus di luar")
        print("       repo supaya tidak pernah ikut ter-commit.")
        return 1
    try:
        tujuan.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"GAGAL: tidak bisa membuat folder {tujuan}: {e}")
        return 1

    print(f"Repo   : {AKAR}")
    print(f"Tujuan : {tujuan}")

    # 1. Ambil dulu supaya cadangan memuat juga commit yang dibuat GitHub
    #    Actions hari ini (snapshot harian). Luring bukan alasan berhenti:
    #    cadangan dari keadaan lokal tetap jauh lebih baik daripada tidak ada.
    if a.tanpa_ambil:
        print("Ambil  : dilewati (--tanpa-ambil)")
    else:
        print("Ambil  : git fetch origin ...")
        hasil = jalankan(["git", "fetch", "origin", "--tags", "--prune"], AKAR)
        if hasil.returncode != 0:
            print("         PERINGATAN: fetch gagal (luring?). Lanjut dengan")
            print("         keadaan lokal saja — isinya mungkin tertinggal.")
        else:
            print("         selesai.")

    jumlah = wajib(["git", "rev-list", "--count", "--all"], AKAR, "menghitung commit")
    kepala = wajib(["git", "rev-parse", "HEAD"], AKAR, "membaca HEAD")

    # 2. Tulis ke berkas sementara lebih dulu. Cadangan lama TIDAK boleh
    #    tertimpa oleh cadangan baru yang ternyata rusak.
    stempel = date.today().isoformat()
    akhir = tujuan / f"{NAMA}-{stempel}.bundle"
    sementara = tujuan / f".{NAMA}-{stempel}.bundle.sedang-dibuat"
    if sementara.exists():
        sementara.unlink()

    print("Buat   : git bundle create --all ...")
    hasil = jalankan(["git", "bundle", "create", str(sementara), "--all"], AKAR)
    if hasil.returncode != 0:
        print("GAGAL: git bundle create")
        print((hasil.stderr or hasil.stdout).strip())
        if sementara.exists():
            sementara.unlink()
        return 1

    # 3. Cadangan yang tidak diperiksa bukan cadangan. Kalau verifikasi
    #    gagal, berkas rusaknya dibuang dan cadangan kemarin tetap utuh.
    print("Periksa: git bundle verify ...")
    periksa = jalankan(["git", "bundle", "verify", str(sementara)], AKAR)
    if periksa.returncode != 0:
        print("GAGAL: bundle tidak lolos verifikasi. Berkas dibuang.")
        print((periksa.stderr or periksa.stdout).strip())
        sementara.unlink()
        return 1

    # 4. Bukti isi: HEAD di dalam bundle harus sama dengan HEAD repo.
    daftar = jalankan(["git", "ls-remote", str(sementara)], AKAR)
    kepala_bundle = ""
    acuan = 0
    for baris in daftar.stdout.splitlines():
        bagian = baris.split()
        if len(bagian) == 2:
            acuan += 1
            if bagian[1] == "HEAD":
                kepala_bundle = bagian[0]
    if kepala_bundle and kepala_bundle != kepala:
        print("GAGAL: HEAD di dalam bundle tidak sama dengan HEAD repo.")
        print(f"       repo  : {kepala}")
        print(f"       bundle: {kepala_bundle}")
        sementara.unlink()
        return 1

    sementara.replace(akhir)

    print("")
    print("SELESAI. Cadangan terbukti utuh:")
    print(f"  berkas : {akhir.name}")
    print(f"  ukuran : {ukuran_rapi(akhir.stat().st_size)}")
    print(f"  commit : {jumlah}")
    print(f"  acuan  : {acuan}")
    print(f"  HEAD   : {kepala[:7]}")
    print("")
    print("Memulihkan (kapan pun, tanpa internet):")
    print(f'  git clone "{akhir.name}" pulih')

    lama = sorted(tujuan.glob(f"{NAMA}-*.bundle"))
    if len(lama) > a.simpan:
        print("")
        print(f"Catatan: ada {len(lama)} berkas cadangan di folder itu (batas ingat"
              f" {a.simpan}).")
        print("         Yang paling tua:")
        for berkas in lama[: len(lama) - a.simpan]:
            print(f"           {berkas.name}")
        print("         Skrip ini sengaja TIDAK menghapus apa pun — hapus sendiri")
        print("         kalau memang sudah tidak perlu.")

    print("")
    print("Satu salinan di satu komputer belum aman. Salin berkas ini ke hard")
    print("disk luar atau penyimpanan awan Anda — itu yang membuatnya cadangan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
