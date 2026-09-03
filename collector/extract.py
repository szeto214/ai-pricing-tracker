"""Ekstraksi terstruktur — pendekatan hybrid.

Urutan percobaan:
  1. adapter khusus situs (collector/adapters/<slug>.py)  -> paling akurat
  2. JSON-LD schema.org Offer/Product                      -> resmi, jarang patah
  3. heuristik DOM                                         -> jaring pengaman

Prinsip: ekstraksi yang gagal TIDAK boleh menggagalkan pengarsipan. Kalau
ketiganya gagal, kita tetap menyimpan teks ternormalisasi + hash, dan struktur
bisa digali ulang dari arsip kapan saja.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from bs4 import BeautifulSoup

from .adapters import get_adapter

CURRENCY_SYMBOLS = {
    "$": "USD", "US$": "USD", "USD": "USD",
    "€": "EUR", "EUR": "EUR",
    "£": "GBP", "GBP": "GBP",
    "rp": "IDR", "IDR": "IDR",
    "¥": "JPY", "JPY": "JPY",
    "A$": "AUD", "C$": "CAD", "S$": "SGD",
}

PRICE_RE = re.compile(
    r"(?P<cur>US\$|A\$|C\$|S\$|\$|€|£|¥|USD|EUR|GBP|IDR|Rp)\s?"
    r"(?P<amt>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"(?P<suffix>\s?[kKmM]\b)?",
    re.I,
)

PERIOD_RE = re.compile(
    r"(?:/|per\s+)\s*(month|mo\b|year|yr\b|annually|seat|user|member|editor|"
    r"credit|request|hour|hr\b|day)",
    re.I,
)

FREE_RE = re.compile(r"^\s*(free|gratis|\$0(?:\.00)?|no cost)\s*$", re.I)

NAME_HINT_RE = re.compile(r"title|name|plan|tier|heading|header", re.I)

_BAD_NAME_RE = re.compile(
    r"^\s*(get started|start free|contact sales|talk to sales|sign up|try |"
    r"learn more|compare|see all|buy now|choose|select|upgrade|book a demo|"
    # Judul penyambung antar-kolom tabel perbandingan, bukan nama paket.
    # Snapshot 29/08 mencatat "Everything in Pro and:" sebagai paket dihapus.
    r"everything in|includes everything|all features|plus everything|up to |save |discount|limited time|promo)",
    re.I,
)


# Judul bagian halaman harga yang bukan nama paket. Snapshot 01/09 mencatat
# "Pricing" (redis) dan "Let's talk numbers" (redis) sebagai paket baru/hilang.
# Dicocokkan UTUH atau sebagai awalan ajakan bicara — bukan pencarian bebas,
# supaya paket sah seperti "Pricing Pro" tidak ikut tersaring.
_GENERIC_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"pricing|pricing plans?|plans?|our plans?|all plans?|compare plans?|compare|"
    r"features?|faq|frequently asked questions|questions?|overview|summary|"
    r"pricing details|how it works|why us|resources|documentation|docs|blog|"
    r"contact|contact us|support|sales"
    r")\s*$"
    r"|^\s*(?:let'?s talk|talk to|need more|still have|got questions|"
    r"ready to|not sure)\b",
    re.I,
)


# Pecahan kalimat yang sempat tercatat sebagai nama paket (openrouter, 02/09).
_FRAGMENTS = {
    "by", "and", "or", "the", "a", "an", "per", "from", "to", "for", "with",
    "in", "on", "at", "of", "up", "new", "more", "all", "each",
}


def clean_plan_name(name: str) -> str:
    """Rapikan sebelum dipakai maupun sebelum divalidasi.

    Tanda baca menggantung membuat nama yang sama terbaca berbeda antar hari
    ("Single Sign-On -" vs "Single Sign-On") lalu tercatat sebagai paket
    hilang + paket baru, padahal tidak terjadi apa-apa.
    """
    return re.sub(r"\s+", " ", (name or "")).strip(" \t-–—:·|")


def _plausible_plan_name(name: str) -> bool:
    """Saringan terakhir sebelum sesuatu diakui sebagai nama paket.

    Penambahan/penghapusan paket dihitung sebagai `price_change` — metrik yang
    menentukan gerbang bulan ke-3. Kalau judul bagian atau nama model ikut
    terbaca sebagai paket, metrik itu jadi menggelembung dan gerbangnya
    kehilangan arti. Lebih baik kehilangan satu paket asli daripada
    memasukkan sepuluh yang palsu.
    """
    n = clean_plan_name(name)
    if not (1 < len(n) <= 48):
        return False
    if n.lower() in _FRAGMENTS:            # "by", "and", "per" — pecahan kalimat
        return False
    if n.endswith(":"):                    # "Everything in Pro and:"
        return False
    if n.endswith("?"):                    # "How much does SonarQube cost?"
        return False                       # pertanyaan FAQ, bukan paket
    if PRICE_RE.search(n):                 # "$4.00" terbaca sebagai nama
        return False
    if _BAD_NAME_RE.match(n):
        return False
    if not re.search(r"[A-Za-z]", n):      # angka atau simbol saja
        return False
    if len(n.split()) > 5:                 # nama paket bukan kalimat
        return False
    if _GENERIC_HEADING_RE.match(n):       # "Pricing", "Let's talk numbers"
        return False
    return True

PERIOD_CANON = {
    "mo": "month", "month": "month",
    "yr": "year", "year": "year", "annually": "year",
    "seat": "seat", "user": "user", "member": "user", "editor": "user",
    "credit": "credit", "request": "request",
    "hour": "hour", "hr": "hour", "day": "day",
}


@dataclass
class Plan:
    name: str
    price_raw: str = ""
    amount: float | None = None
    currency: str | None = None
    period: str | None = None
    features: list[str] = field(default_factory=list)


def parse_price(text: str) -> tuple[float | None, str | None, str]:
    m = PRICE_RE.search(text or "")
    if not m:
        return None, None, ""
    cur = CURRENCY_SYMBOLS.get(m.group("cur"), CURRENCY_SYMBOLS.get(
        m.group("cur").upper(), None))
    amt_s = m.group("amt").replace(",", "")
    try:
        amt = float(amt_s)
    except ValueError:
        return None, cur, m.group(0)
    suffix = (m.group("suffix") or "").strip().lower()
    if suffix == "k":
        amt *= 1_000
    elif suffix == "m":
        amt *= 1_000_000
    return amt, cur, m.group(0).strip()


def parse_period(text: str) -> str | None:
    m = PERIOD_RE.search(text or "")
    if not m:
        return None
    return PERIOD_CANON.get(m.group(1).lower().rstrip("."), m.group(1).lower())


# --------------------------------------------------------------------------- #
# 2. JSON-LD
# --------------------------------------------------------------------------- #
def _iter_jsonld(raw_html: str):
    # script sudah dibuang dari soup bersih, jadi baca dari HTML mentah.
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        raw_html, re.I | re.S,
    ):
        block = m.group(1).strip()
        try:
            data = json.loads(block)
        except Exception:  # noqa: BLE001
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))


def extract_jsonld(raw_html: str) -> list[Plan]:
    plans: list[Plan] = []
    for node in _iter_jsonld(raw_html):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        types = [str(x).lower() for x in types if x]
        if not any(x in ("offer", "aggregateoffer", "product", "service") for x in types):
            continue
        offers = node.get("offers")
        candidates = []
        if offers:
            candidates = offers if isinstance(offers, list) else [offers]
        elif "price" in node or "lowPrice" in node:
            candidates = [node]
        for off in candidates:
            if not isinstance(off, dict):
                continue
            price = off.get("price") or off.get("lowPrice")
            if price in (None, ""):
                continue
            try:
                amount = float(str(price).replace(",", ""))
            except ValueError:
                continue
            name = (off.get("name") or node.get("name") or "").strip()
            if not name:
                continue
            plans.append(Plan(
                name=name[:80],
                price_raw=str(price),
                amount=amount,
                currency=off.get("priceCurrency") or node.get("priceCurrency"),
                period=_jsonld_period(off),
            ))
    return _dedupe(plans)


def _jsonld_period(off: dict) -> str | None:
    spec = off.get("priceSpecification")
    if isinstance(spec, dict):
        unit = spec.get("billingDuration") or spec.get("unitText")
        if unit:
            return str(unit).lower()
    return None


# --------------------------------------------------------------------------- #
# 3. Heuristik DOM
# --------------------------------------------------------------------------- #
def _card_for(node, max_up: int = 8):
    """Naik dari elemen berisi harga sampai ketemu wadah seukuran 'kartu paket'.

    Ambil wadah TERKECIL yang punya judul. Versi sebelumnya terus naik dan
    memilih wadah terbesar, sehingga kartu ringkas seperti
    "Starter / Free / 1 project" tertelan oleh grid induknya dan namanya
    terbaca dari judul halaman.
    """
    cur = node
    with_list = None
    loose = None
    for _ in range(max_up):
        if cur is None or cur.name in (None, "body", "html"):
            break
        text = cur.get_text(" ", strip=True)
        n = len(text)
        if n > 2500:
            break
        if n >= 12:
            if cur.find(["h1", "h2", "h3", "h4", "h5", "h6"]) is not None:
                return cur
            if with_list is None and cur.find("li") is not None:
                with_list = cur
            if loose is None and n >= 40:
                loose = cur
        cur = cur.parent
    return with_list or loose


def _plan_name(card) -> str:
    """Kembalikan "" kalau tidak ada kandidat yang meyakinkan — bukan tebakan."""
    for tag in card.find_all(["h1", "h2", "h3", "h4", "h5", "h6"], limit=4):
        t = tag.get_text(" ", strip=True)
        if _plausible_plan_name(t):
            return clean_plan_name(t)
    for tag in card.find_all(attrs={"class": NAME_HINT_RE}, limit=8):
        t = tag.get_text(" ", strip=True)
        if _plausible_plan_name(t):
            return clean_plan_name(t)
    # Cadangan: baris pertama kartu — TETAP harus lolos saringan yang sama.
    # Versi lama mengembalikan baris pertama apa adanya, dan itulah yang
    # memasukkan "$4.00" sebagai nama paket pada snapshot 29/08.
    first = card.get_text("\n", strip=True).split("\n")[0]
    return clean_plan_name(first) if _plausible_plan_name(first) else ""


def _features(card, limit: int = 25) -> list[str]:
    out = []
    for li in card.find_all("li", limit=limit * 2):
        t = li.get_text(" ", strip=True)
        if t and 2 < len(t) <= 200:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def extract_dom(soup: BeautifulSoup) -> list[Plan]:
    body = soup.body or soup
    seen_cards: set[int] = set()
    plans: list[Plan] = []

    # Kumpulkan kandidat dalam urutan dokumen supaya urutan paket masuk akal.
    candidates: list[tuple] = []
    for string in body.find_all(string=True):
        parent = string.parent
        if parent is None:
            continue
        if PRICE_RE.search(string):
            candidates.append((parent, False))
        elif FREE_RE.match(string):
            # elemen yang isinya PERSIS "Free"/"$0" -> penanda paket gratis.
            # "Start free trial" tidak lolos karena regex-nya di-anchor.
            candidates.append((parent, True))

    for node, is_free in candidates:
        card = _card_for(node)
        if card is None or id(card) in seen_cards:
            continue
        seen_cards.add(id(card))

        card_text = card.get_text(" ", strip=True)
        amount, currency, raw = parse_price(card_text)
        if amount is None:
            if not is_free:
                continue
            amount, raw = 0.0, "Free"

        name = _plan_name(card)
        if not name or name == "?":
            continue
        plans.append(Plan(
            name=name,
            price_raw=raw,
            amount=amount,
            currency=currency,
            period=parse_period(card_text),
            features=_features(card),
        ))
        if len(plans) >= 20:
            break

    return _dedupe(plans)


# --------------------------------------------------------------------------- #
# Tabel harga (penting untuk halaman harga API per-token)
# --------------------------------------------------------------------------- #
def extract_tables(soup: BeautifulSoup, max_tables: int = 8,
                   max_rows: int = 80, max_cols: int = 12) -> list[dict]:
    tables = []
    for table in (soup.body or soup).find_all("table", limit=max_tables * 3):
        rows = []
        for tr in table.find_all("tr", limit=max_rows):
            cells = [
                c.get_text(" ", strip=True)[:160]
                for c in tr.find_all(["th", "td"], limit=max_cols)
            ]
            if any(cells):
                rows.append(cells)
        if len(rows) < 2:
            continue
        caption = table.find("caption")
        tables.append({
            "caption": caption.get_text(" ", strip=True)[:120] if caption else "",
            "rows": rows,
        })
        if len(tables) >= max_tables:
            break
    return tables


# --------------------------------------------------------------------------- #
def _dedupe(plans: list[Plan]) -> list[Plan]:
    out, seen = [], set()
    for p in plans:
        key = (p.name.strip().lower(), p.amount, p.period)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def extract(slug: str, soup: BeautifulSoup, raw_html: str) -> dict:
    """Kembalikan dict siap-serialisasi."""
    errors: list[str] = []

    adapter = get_adapter(slug)
    if adapter is not None:
        try:
            plans = adapter(soup, raw_html)
            if plans:
                return _result(plans, f"adapter:{slug}", "high",
                               extract_tables(soup), errors)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"adapter:{slug} gagal: {type(exc).__name__}: {exc}")

    try:
        plans = extract_jsonld(raw_html)
        if len(plans) >= 2:
            return _result(plans, "jsonld", "medium", extract_tables(soup), errors)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"jsonld gagal: {type(exc).__name__}: {exc}")
        plans = []

    jsonld_plans = plans
    try:
        dom_plans = extract_dom(soup)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"dom gagal: {type(exc).__name__}: {exc}")
        dom_plans = []

    if dom_plans:
        conf = "medium" if len(dom_plans) >= 2 else "low"
        return _result(dom_plans, "dom", conf, extract_tables(soup), errors)
    if jsonld_plans:
        return _result(jsonld_plans, "jsonld", "low", extract_tables(soup), errors)

    tables = extract_tables(soup)
    conf = "low" if tables else "none"
    return _result([], "none", conf, tables, errors)


def _result(plans, extractor, confidence, tables, errors) -> dict:
    return {
        "extractor": extractor,
        "confidence": confidence,
        "plans": [asdict(p) for p in plans],
        "tables": tables,
        "extract_errors": errors,
    }
