"""Konfigurasi global + pemuat daftar target."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# APT_DATA_DIR dipakai oleh test supaya tidak mengotori data produksi.
DATA_DIR = Path(os.environ.get("APT_DATA_DIR") or (ROOT / "data"))
CURRENT_DIR = DATA_DIR / "current"
RAW_DIR = DATA_DIR / "raw"
CHANGES_DIR = DATA_DIR / "changes"
RUNS_DIR = DATA_DIR / "runs"
TARGETS_FILE = Path(os.environ.get("APT_TARGETS_FILE")
                    or (ROOT / "targets" / "targets.yaml"))

CHANGES_LOG = CHANGES_DIR / "changes.jsonl"

# --- identitas bot -----------------------------------------------------------
# Ganti CONTACT_URL setelah domain final dibeli. Bot HARUS bisa dihubungi.
PROJECT_NAME = os.environ.get("APT_PROJECT_NAME", "ai-pricing-tracker")
CONTACT_URL = os.environ.get(
    "APT_CONTACT_URL", "https://github.com/CHANGEME/ai-pricing-tracker"
)
USER_AGENT = (
    f"{PROJECT_NAME}/0.1 (+{CONTACT_URL}) "
    "price-archive-bot; 1 request per page per day"
)

# --- batas sopan santun ------------------------------------------------------
REQUEST_TIMEOUT = 30.0          # detik
JS_TIMEOUT = 60.0               # detik, halaman render=js butuh lebih lama
JS_SETTLE_MS = 2500             # jeda tetap setelah DOM siap, untuk harga
                                # yang baru muncul setelah fetch klien
MAX_CONCURRENCY = 5             # permintaan paralel lintas host
# APT_MIN_INTERVAL hanya untuk test lokal. JANGAN diturunkan di produksi.
PER_HOST_MIN_INTERVAL = float(os.environ.get("APT_MIN_INTERVAL", "6.0"))
DEFAULT_CRAWL_DELAY = PER_HOST_MIN_INTERVAL  # kalau robots.txt tidak menyebut
MAX_RETRIES = 2
RETRY_BACKOFF = 8.0             # detik, dikalikan percobaan ke-n
MAX_BYTES = 6 * 1024 * 1024     # tolak halaman > 6 MB

# --- versi pembaca angka ------------------------------------------------------
# NAIKKAN setiap kali cara MEMBACA angka berubah: regex harga, pembersihan nama
# paket, aturan ekstraksi — apa pun yang bisa membuat halaman yang SAMA PERSIS
# terbaca dengan angka berbeda.
#
# Kenapa ini ada. Pada 04/09/2026 PRICE_RE dilebarkan dari 2 ke 6 desimal supaya
# $0.00012 tidak lagi terbaca $0.00. Perbaikan itu benar, tapi rekaman kemarin
# sudah tersimpan dengan pembacaan lama. Keesokan harinya pembanding melihat
# $0.00 -> $0.0045 dan mencatatnya sebagai kenaikan harga: 26 perubahan palsu
# di 7 halaman, padahal tidak satu vendor pun mengubah harga. Yang berubah
# pembacanya, bukan harganya.
#
# Dengan penanda versi, hari pertama setelah pembaca berubah diklasifikasikan
# sebagai `parser_upgrade` dan TIDAK pernah dihitung sebagai perubahan harga.
# Peristiwanya tetap tercatat lengkap, jadi tidak ada data yang hilang.
#   2 (04/09) PRICE_RE dilebarkan ke 6 desimal
#   3 (05/09) kartu tanpa harga tidak lagi ditebak sebagai "Free"
PARSER_VERSION = 3


@dataclass
class Target:
    slug: str
    name: str
    url: str
    vendor: str = ""
    category: str = "uncategorized"
    render: str = "static"
    currency: str = "USD"
    enabled: bool = True
    notes: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def host(self) -> str:
        from urllib.parse import urlsplit

        return urlsplit(self.url).netloc.lower()

    @property
    def fetch_key(self) -> str:
        return fetch_key(self.url)


def fetch_key(url: str) -> str:
    """URL sebagaimana dilihat server penerima permintaan.

    Fragment (`#api`) TIDAK PERNAH dikirim ke server, jadi dua target yang
    hanya berbeda di fragment adalah satu halaman yang sama. Tanpa
    penyeragaman ini, `anthropic-claude` dan `anthropic-api` mengambil
    https://www.anthropic.com/pricing dua kali dalam satu eksekusi —
    melanggar aturan kita sendiri: maksimal sekali sehari per halaman.
    """
    from urllib.parse import urlsplit, urlunsplit

    p = urlsplit((url or "").strip())
    path = p.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, p.query, ""))


def load_targets(path: Path | None = None) -> list[Target]:
    path = path or TARGETS_FILE
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}
    out: list[Target] = []
    seen: set[str] = set()

    for item in raw.get("targets") or []:
        merged = {**defaults, **item}
        slug = merged.get("slug")
        if not slug:
            raise ValueError(f"Target tanpa slug: {item!r}")
        if slug in seen:
            raise ValueError(f"Slug ganda: {slug}")
        seen.add(slug)

        known = {
            "slug",
            "name",
            "url",
            "vendor",
            "category",
            "render",
            "currency",
            "enabled",
            "notes",
        }
        extra = {k: v for k, v in merged.items() if k not in known}
        out.append(
            Target(
                slug=slug,
                name=merged.get("name") or slug,
                url=merged["url"],
                vendor=merged.get("vendor", ""),
                category=merged.get("category", "uncategorized"),
                render=merged.get("render", "static"),
                currency=merged.get("currency", "USD"),
                enabled=bool(merged.get("enabled", True)),
                notes=merged.get("notes", "") or "",
                extra=extra,
            )
        )
    return out


def ensure_dirs() -> None:
    for d in (DATA_DIR, CURRENT_DIR, RAW_DIR, CHANGES_DIR, RUNS_DIR):
        d.mkdir(parents=True, exist_ok=True)
