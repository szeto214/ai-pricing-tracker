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


def _strip_noise(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    for node in soup.find_all(string=lambda s: isinstance(s, Comment)):
        node.extract()
    for tag in soup.find_all(attrs={"hidden": True}):
        tag.decompose()
    for tag in soup.find_all(attrs={"aria-hidden": "true"}):
        # ikon dekoratif; teks harga tidak pernah aria-hidden
        if len(tag.get_text(strip=True)) < 40:
            tag.decompose()
    for tag in soup.find_all(True):
        ident = " ".join(
            filter(None, [tag.get("id") or "", " ".join(tag.get("class") or [])])
        )
        if ident and _DROP_PATTERN.search(ident):
            tag.decompose()


def clean_html(raw_html: str) -> tuple[BeautifulSoup, str]:
    """Kembalikan (soup bersih, html bersih untuk arsip)."""
    soup = BeautifulSoup(raw_html, "lxml")
    _strip_noise(soup)

    archive = BeautifulSoup(str(soup), "lxml")
    for tag in archive.find_all(True):
        tag.attrs = {k: v for k, v in tag.attrs.items() if k in _KEEP_ATTRS}
    return soup, str(archive)


def to_text(soup: BeautifulSoup) -> str:
    body = soup.body or soup
    text = body.get_text("\n")
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("​", "").replace("﻿", "")

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
