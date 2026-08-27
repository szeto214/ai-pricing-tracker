"""Pengambil halaman yang sopan.

Aturan yang ditegakkan modul ini (lihat bagian etika di README):
  * robots.txt dihormati; kalau melarang -> dilewati, tidak diambil.
  * RFC 9309: robots.txt 4xx  -> boleh semua;  5xx / gagal -> jangan ambil.
  * satu permintaan per halaman per hari (dijaga di run.py).
  * jeda antar permintaan ke host yang sama minimal PER_HOST_MIN_INTERVAL
    atau Crawl-delay dari robots.txt, mana yang lebih besar.
  * User-Agent menyebut nama proyek + URL kontak.
  * tidak pernah mengambil di balik login/paywall (tanggung jawab daftar target).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from . import config

UA_TOKEN = "ai-pricing-tracker"


@dataclass
class FetchResult:
    ok: bool
    status: str                 # ok | robots_denied | http_error | network_error
                                # | too_large | skipped_render
    http_status: int | None = None
    html: str = ""
    final_url: str = ""
    reason: str = ""
    elapsed_ms: int = 0
    robots_note: str = ""


class HostGate:
    """Serialisasi + jeda minimum per host."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self._delay: dict[str, float] = {}

    def lock(self, host: str) -> asyncio.Lock:
        return self._locks.setdefault(host, asyncio.Lock())

    def set_delay(self, host: str, delay: float) -> None:
        self._delay[host] = max(delay, config.PER_HOST_MIN_INTERVAL)

    async def wait(self, host: str) -> None:
        delay = self._delay.get(host, config.PER_HOST_MIN_INTERVAL)
        last = self._last.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < delay:
                await asyncio.sleep(delay - gap)
        self._last[host] = time.monotonic()


class RobotsCache:
    def __init__(self, client: httpx.AsyncClient, gate: HostGate) -> None:
        self._client = client
        self._gate = gate
        self._cache: dict[str, tuple[RobotFileParser | None, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, url: str) -> tuple[RobotFileParser | None, str]:
        parts = urlsplit(url)
        host_key = f"{parts.scheme}://{parts.netloc}"
        lock = self._locks.setdefault(host_key, asyncio.Lock())
        async with lock:
            if host_key in self._cache:
                return self._cache[host_key]
            result = await self._load(parts)
            self._cache[host_key] = result
            return result

    async def _load(self, parts) -> tuple[RobotFileParser | None, str]:
        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        try:
            await self._gate.wait(parts.netloc.lower())
            resp = await self._client.get(robots_url)
        except Exception as exc:  # noqa: BLE001
            return None, f"robots.txt tidak terbaca ({type(exc).__name__}) -> tidak diambil"

        if resp.status_code >= 500:
            return None, f"robots.txt HTTP {resp.status_code} -> tidak diambil"
        if resp.status_code >= 400:
            rp = RobotFileParser()
            rp.parse([])           # kosong = izinkan semua
            return rp, f"robots.txt HTTP {resp.status_code} -> dianggap mengizinkan"

        rp = RobotFileParser()
        try:
            rp.parse(resp.text.splitlines())
        except Exception as exc:  # noqa: BLE001
            return None, f"robots.txt gagal diurai ({type(exc).__name__}) -> tidak diambil"
        return rp, "robots.txt terbaca"


async def fetch(
    url: str,
    *,
    client: httpx.AsyncClient,
    robots: RobotsCache,
    gate: HostGate,
    render: str = "static",
) -> FetchResult:
    started = time.monotonic()
    host = urlsplit(url).netloc.lower()

    rp, note = await robots.get(url)
    if rp is None:
        return FetchResult(False, "robots_denied", reason=note, robots_note=note)
    if not rp.can_fetch(UA_TOKEN, url) or not rp.can_fetch("*", url):
        msg = "robots.txt melarang URL ini"
        return FetchResult(False, "robots_denied", reason=msg, robots_note=note)

    delay = None
    try:
        delay = rp.crawl_delay(UA_TOKEN) or rp.crawl_delay("*")
    except Exception:  # noqa: BLE001
        delay = None
    gate.set_delay(host, float(delay) if delay else config.DEFAULT_CRAWL_DELAY)

    if render == "js":
        return await _fetch_js(url, gate=gate, host=host, started=started, note=note)

    last_reason = ""
    for attempt in range(config.MAX_RETRIES + 1):
        async with gate.lock(host):
            await gate.wait(host)
            try:
                resp = await client.get(url)
            except Exception as exc:  # noqa: BLE001
                last_reason = f"{type(exc).__name__}: {exc}"
                resp = None

        if resp is None:
            if attempt < config.MAX_RETRIES:
                await asyncio.sleep(config.RETRY_BACKOFF * (attempt + 1))
                continue
            return FetchResult(
                False, "network_error", reason=last_reason,
                elapsed_ms=_ms(started), robots_note=note,
            )

        if resp.status_code in (429, 500, 502, 503, 504):
            last_reason = f"HTTP {resp.status_code}"
            if attempt < config.MAX_RETRIES:
                wait = config.RETRY_BACKOFF * (attempt + 1)
                ra = resp.headers.get("retry-after")
                if ra and ra.isdigit():
                    wait = max(wait, min(float(ra), 120.0))
                await asyncio.sleep(wait)
                continue
            return FetchResult(
                False, "http_error", http_status=resp.status_code,
                reason=last_reason, elapsed_ms=_ms(started), robots_note=note,
            )

        if resp.status_code >= 400:
            return FetchResult(
                False, "http_error", http_status=resp.status_code,
                reason=f"HTTP {resp.status_code}", elapsed_ms=_ms(started),
                robots_note=note,
            )

        if len(resp.content) > config.MAX_BYTES:
            return FetchResult(
                False, "too_large", http_status=resp.status_code,
                reason=f"{len(resp.content)} byte", elapsed_ms=_ms(started),
                robots_note=note,
            )

        return FetchResult(
            True, "ok", http_status=resp.status_code, html=resp.text,
            final_url=str(resp.url), elapsed_ms=_ms(started), robots_note=note,
        )

    return FetchResult(False, "network_error", reason=last_reason or "tidak diketahui",
                       elapsed_ms=_ms(started), robots_note=note)


async def _fetch_js(url: str, *, gate: HostGate, host: str, started: float,
                    note: str) -> FetchResult:
    try:
        from playwright.async_api import async_playwright
    except Exception:  # noqa: BLE001
        return FetchResult(
            False, "skipped_render",
            reason="playwright tidak terpasang; target render=js dilewati",
            elapsed_ms=_ms(started), robots_note=note,
        )

    async with gate.lock(host):
        await gate.wait(host)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                ctx = await browser.new_context(user_agent=config.USER_AGENT)
                page = await ctx.new_page()
                await page.goto(url, wait_until="networkidle",
                                timeout=int(config.REQUEST_TIMEOUT * 1000))
                html = await page.content()
                final = page.url
                await browser.close()
        except Exception as exc:  # noqa: BLE001
            return FetchResult(
                False, "network_error", reason=f"{type(exc).__name__}: {exc}",
                elapsed_ms=_ms(started), robots_note=note,
            )

    return FetchResult(True, "ok", http_status=200, html=html, final_url=final,
                       elapsed_ms=_ms(started), robots_note=note)


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=config.REQUEST_TIMEOUT,
        follow_redirects=True,
        http2=False,
    )


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
