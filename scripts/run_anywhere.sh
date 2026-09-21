#!/usr/bin/env bash
# Menjalankan pengambilan harian dari MESIN LINUX BIASA, bukan GitHub Actions.
#
# Kenapa ini ada. Ketentuan GitHub melarang, khusus runner GitHub, "any other
# activity unrelated to the production, testing, deployment, or publication of
# the software project associated with the repository", dengan contoh
# infrastruktur serverless. Pengambil harga harian ada di wilayah abu-abu.
# Kalau suatu hari workflow dihentikan, arsip ini tidak boleh ikut berhenti:
# satu hari yang hilang tidak bisa dibeli kembali. Skrip ini adalah jalan
# keluarnya, dan dirancang supaya bisa diaktifkan dalam waktu kurang dari
# satu jam di VPS mana pun (Rp 50-100 ribu/bulan sudah cukup).
#
# Yang TIDAK berubah di sini: etika pengambilan. Penjaga sekali-sehari,
# robots.txt, jeda antar permintaan, dan User-Agent ber-kontak semuanya hidup
# di dalam kode kolektor — skrip ini hanya memanggilnya.
#
# Cara pakai:
#   git clone https://github.com/szeto214/ai-pricing-tracker.git
#   cd ai-pricing-tracker
#   export APT_CONTACT_URL="https://github.com/szeto214/ai-pricing-tracker"
#   bash scripts/run_anywhere.sh
#
# Supaya bisa mendorong hasilnya, remote `origin` harus sudah bisa menulis
# (SSH key, atau URL HTTPS berisi token). Skrip ini SENGAJA tidak pernah
# meminta, menyimpan, atau mencetak kredensial apa pun.
#
# Uji tanpa menyentuh situs vendor sama sekali:
#   APT_TARGETS_FILE=/path/targets-uji.yaml APT_DATA_DIR=/tmp/uji \
#     APT_SKIP_PUSH=1 bash scripts/run_anywhere.sh
set -euo pipefail

cd "$(dirname "$0")/.."
AKAR="$(pwd)"
echo "== ai-pricing-tracker — jalan di luar GitHub Actions"
echo "   folder      : $AKAR"
echo "   tanggal UTC : $(date -u +%F)"

# --- 1. Python + dependensi --------------------------------------------------
PY="${APT_PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "!! $PY tidak ada"; exit 1; }
if [ ! -d .venv ]; then
  echo "-- membuat .venv"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q -r requirements.txt

# Chromium hanya untuk target `render: js`. Kalau gagal dipasang, kolektor
# tetap jalan: target itu dilewati dengan status skipped_render, arsipnya
# bolong di halaman tersebut saja — bukan hari yang hilang.
if [ "${APT_SKIP_PLAYWRIGHT:-0}" != "1" ]; then
  pip install -q playwright==1.62.0 || true
  python -m playwright install --with-deps chromium >/dev/null 2>&1 || \
    echo "!! Chromium gagal dipasang — target render:js akan dilewati"
fi

# --- 2. Selalu mulai dari arsip terbaru --------------------------------------
# Penjaga sekali-sehari membaca data/runs/<tgl>.json. Tanpa pull lebih dulu,
# mesin ini tidak tahu GitHub Actions sudah mengambil hari ini, dan halaman
# yang sama bisa diambil dua kali dalam sehari.
if [ "${APT_SKIP_GIT:-0}" != "1" ]; then
  git pull --rebase --autostash origin "$(git rev-parse --abbrev-ref HEAD)"
fi

# --- 3. Ambil -----------------------------------------------------------------
export APT_CONTACT_URL="${APT_CONTACT_URL:-https://github.com/szeto214/ai-pricing-tracker}"
python -m collector.run

# --- 4. Simpan arsip DULU, halaman publik belakangan -------------------------
# Urutan ini disengaja dan sama dengan collect.yml: arsip adalah aset, halaman
# publik hanya tampilannya. Kalau pembangun halaman rusak, satu hari arsip pun
# tidak boleh hilang.
PESAN="$(python scripts/summarize_run.py | sed -n 's/^message=//p')"
[ -n "$PESAN" ] || PESAN="data: snapshot (ringkasan tidak tersedia)"

if [ "${APT_SKIP_PUSH:-0}" = "1" ]; then
  echo "-- APT_SKIP_PUSH=1: arsip tidak di-commit (mode uji)"
else
  python scripts/push_snapshot.py "$PESAN"
  python scripts/build_site.py --commit || \
    echo "!! pembangun halaman gagal — arsip tetap aman, halaman menyusul besok"
fi

python scripts/summarize_run.py --markdown | head -20
echo "== selesai"
