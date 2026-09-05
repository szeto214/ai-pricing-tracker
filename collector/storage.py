"""Tata letak penyimpanan.

    data/current/<slug>.json   keadaan terkini (hanya field turunan isi —
                               TIDAK ada timestamp, supaya diff git = perubahan nyata)
    data/current/<slug>.txt    teks halaman ternormalisasi (ini yang bikin
                               `git diff` langsung terbaca manusia)
    data/raw/<slug>/<tgl>.html.gz   HTML bersih, HANYA ditulis saat isi berubah
    data/changes/changes.jsonl      log perubahan append-only
    data/changes/corrections.jsonl  catatan koreksi, juga append-only:
                                    peristiwa yang TERBUKTI bukan perubahan
                                    harga sungguhan. Log aslinya tidak pernah
                                    diubah — arsip tidak boleh ditulis ulang;
                                    yang berubah hanya cara menghitungnya.
    data/runs/<tgl>.json            log eksekusi harian (status tiap target)

Riwayat git-lah arsipnya. Berkas `current` sengaja bebas timestamp supaya
commit harian tidak menghasilkan diff palsu.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config
from .normalize import gzip_bytes


def _write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def current_paths(slug: str) -> tuple[Path, Path]:
    return config.CURRENT_DIR / f"{slug}.json", config.CURRENT_DIR / f"{slug}.txt"


def load_current(slug: str) -> dict | None:
    jpath, tpath = current_paths(slug)
    if not jpath.exists():
        return None
    try:
        data = json.loads(jpath.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    data["_text"] = tpath.read_text(encoding="utf-8") if tpath.exists() else ""
    return data


def save_current(record: dict, text: str) -> None:
    jpath, tpath = current_paths(record["slug"])
    _write_json(jpath, record)
    tpath.parent.mkdir(parents=True, exist_ok=True)
    tpath.write_text(text, encoding="utf-8")


def save_raw_snapshot(slug: str, date: str, archive_html: str) -> Path:
    path = config.RAW_DIR / slug / f"{date}.html.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip_bytes(archive_html))
    return path


def append_changes(entries: list[dict]) -> None:
    if not entries:
        return
    config.CHANGES_DIR.mkdir(parents=True, exist_ok=True)
    with config.CHANGES_LOG.open("a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def save_run_log(date: str, log: dict) -> Path:
    path = config.RUNS_DIR / f"{date}.json"
    _write_json(path, log)
    return path


CORRECTIONS_LOG = config.CHANGES_DIR / "corrections.jsonl"


def load_corrections() -> set[tuple[str, str, str]]:
    """(tanggal, slug, kind) yang terbukti bukan perubahan sungguhan.

    Arsip tidak pernah ditulis ulang. Kalau kelak terbukti sebuah peristiwa
    lahir dari cacat pembaca dan bukan dari vendor, catatannya ditambahkan di
    sini dan penghitunglah yang menyesuaikan. Dengan begitu riwayatnya tetap
    jujur: terlihat apa yang dulu tercatat, DAN terlihat apa yang kemudian
    diketahui salah.
    """
    out: set[tuple[str, str, str]] = set()
    if not CORRECTIONS_LOG.exists():
        return out
    for line in CORRECTIONS_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("date") and e.get("slug"):
            out.add((e["date"], e["slug"], e.get("kind", "price_change")))
    return out


def load_run_log(date: str) -> dict | None:
    path = config.RUNS_DIR / f"{date}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
