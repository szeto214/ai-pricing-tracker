"""Ubah HTML mentah jadi teks yang stabil + hash.

Tujuan utamanya satu: hash yang sama untuk halaman yang isinya sama, meski
markup-nya berubah karena nonce, id acak, atau build hash. Kalau ini gagal,
arsipnya akan penuh "perubahan" palsu dan jadi tidak berguna.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import unicodedata

from bs4 import BeautifulSoup, Comment

_DROP_TAGS = (
    "script", "style", "noscript", "svg", "canvas", "template",
    "iframe", "object", "embed", "link", "meta", "picture", "source",
    "audio", "video", "map", "area",
)

# Wadah yang hampir selalu bising dan tidak ada hubungannya dengan harga.
_DROP_PATTERN = re.compile(
    r"cookie|consent|gdpr|onetrust|osano|cky-|announcement-bar|"
    r"skip-to-content|newsletter-popup|intercom|drift-|hubspot-messages",
    re.I,
)

# Halaman harga sering memuat CONTOH kredensial (connection string, API key
# di potongan kode). Itu bukan rahasia siapa pun — tapi push protection GitHub
# tidak bisa membedakannya, dan satu string semacam itu menolak SELURUH commit
# harian. Satu halaman bising tidak boleh menghapus arsip 67 halaman lain,
# jadi token berbentuk kredensial disamarkan sebelum disimpan.
_SECRETS = [
    re.compile(r"\bpscale_(?:pw|tkn)_[A-Za-z0-9._\-]{16,}", re.I),
    re.compile(r"\bsk-ant-[A-Za-z0-9._\-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z._\-]{30,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bSG\.[A-Za-z0-9._\-]{20,}"),
    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    # bentuk umum: <vendor>_<jenis>_<token panjang>
    re.compile(r"\b[A-Za-z][A-Za-z0-9]{1,15}_"
               r"(?:pw|pwd|password|key|token|tkn|secret|api)_"
               r"[A-Za-z0-9._\-]{16,}", re.I),
]

# Token yang berubah tiap muat halaman -> ganti placeholder, jangan dihapus,
# supaya struktur baris tetap sama.
_VOLATILE = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b"), "<timestamp>"),
    (re.compile(r"\b\d{10,13}\b"), "<epoch>"),
]

_WS = re.compile(r"[ \t   ]+")
_MULTI_NL = re.compile(r"\n{3,}")

_KEEP_ATTRS = {"class", "id", "href", "aria-label", "alt", "title", "itemprop",
               "data-testid"}


def _alive(tag) -> bool:
    """Tag yang sudah di-decompose bersama induknya tidak boleh disentuh lagi.

    bs4 menghancurkan seluruh keturunan saat induknya di-decompose, dan
    `tag.attrs` jadi None. Memanggil .get()/.get_text() pada tag mati itu
    melempar AttributeError dan menggagalkan seluruh target.
    """
    return not getattr(tag, "decomposed", False) and getattr(tag, "attrs", None) is not None


def _decompose_all(tags) -> None:
    """Kumpulkan dulu, baru hancurkan — jangan decompose sambil iterasi."""
    for tag in tags:
        if not _alive(tag):
            continue
        try:
            tag.decompose()
        except Exception:  # noqa: BLE001 — satu tag rusak jangan menjatuhkan target
            pass


def _strip_noise(soup: BeautifulSoup) -> None:
    _decompose_all(list(soup.find_all(_DROP_TAGS)))

    for node in soup.find_all(string=lambda s: isinstance(s, Comment)):
        node.extract()

    _decompose_all(list(soup.find_all(attrs={"hidden": True})))

    # ikon dekoratif; teks harga tidak pernah aria-hidden
    _decompose_all([
        t for t in soup.find_all(attrs={"aria-hidden": "true"})
        if _alive(t) and len(t.get_text(strip=True)) < 40
    ])

    doomed = []
    for tag in soup.find_all(True):
        if not _alive(tag):
            continue
        ident = " ".join(
            filter(None, [tag.get("id") or "", " ".join(tag.get("class") or [])])
        )
        if ident and _DROP_PATTERN.search(ident):
            doomed.append(tag)
    _decompose_all(doomed)


def clean_html(raw_html: str) -> tuple[BeautifulSoup, str]:
    """Kembalikan (soup bersih, html bersih untuk arsip)."""
    soup = BeautifulSoup(raw_html, "lxml")
    _strip_noise(soup)

    archive = BeautifulSoup(str(soup), "lxml")
    for tag in archive.find_all(True):
        if not _alive(tag):
            continue
        tag.attrs = {k: v for k, v in tag.attrs.items() if k in _KEEP_ATTRS}
    return soup, str(archive)


def to_text(soup: BeautifulSoup) -> str:
    body = soup.body or soup
    text = body.get_text("\n")
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("​", "").replace("﻿", "")

    # Urutan penting: samarkan kredensial DULU, sebelum aturan <hex> sempat
    # memotong sebagian token dan menyisakan pecahan yang masih terdeteksi.
    for pattern in _SECRETS:
        text = pattern.sub("<secret-redacted>", text)

    for pattern, repl in _VOLATILE:
        text = pattern.sub(repl, text)

    lines = []
    for line in text.split("\n"):
        line = _WS.sub(" ", line).strip()
        if line:
            lines.append(line)

    out = "\n".join(lines)
    return _MULTI_NL.sub("\n\n", out).strip() + "\n"


def sha256(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def gzip_bytes(text: str) -> bytes:
    # mtime=0 supaya berkas gz identik untuk isi identik (git-friendly).
    return gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)


def process(raw_html: str) -> dict:
    soup, archive_html = clean_html(raw_html)
    text = to_text(soup)
    return {
        "soup": soup,
        "archive_html": archive_html,
        "text": text,
        "content_hash": sha256(text),
        "raw_hash": sha256(raw_html),
        "text_bytes": len(text.encode("utf-8")),
    }
