"""Pembanding snapshot: apa yang berubah dibanding rekaman terakhir."""

from __future__ import annotations

import difflib


def _plan_key(plan: dict) -> str:
    return (plan.get("name") or "").strip().lower()


def _plan_index(plans: list[dict]) -> dict[str, dict]:
    """Saring nama paket yang tidak masuk akal di KEDUA sisi perbandingan.

    Penambahan/penghapusan paket dihitung sebagai `price_change`, dan itu
    metrik yang menentukan gerbang bulan ke-3. Judul bagian seperti
    "Everything in Pro and:" atau nama model yang kebetulan terbaca sebagai
    paket akan menggelembungkan metrik itu. Karena penyaringan dilakukan di
    kedua sisi, sampah lama di rekaman kemarin tidak ikut tercatat sebagai
    "paket dihapus" hari ini.
    """
    from .extract import _plausible_plan_name

    out: dict[str, dict] = {}
    for p in plans or []:
        key = _plan_key(p)
        if key and key not in out and _plausible_plan_name(p.get("name", "")):
            out[key] = p
    return out


def diff_plans(old_plans: list[dict], new_plans: list[dict]) -> list[dict]:
    old, new = _plan_index(old_plans), _plan_index(new_plans)
    events: list[dict] = []

    for key, plan in new.items():
        if key not in old:
            events.append({
                "type": "plan_added",
                "plan": plan.get("name"),
                "price": plan.get("price_raw"),
                "amount": plan.get("amount"),
                "period": plan.get("period"),
            })

    for key, plan in old.items():
        if key not in new:
            events.append({
                "type": "plan_removed",
                "plan": plan.get("name"),
                "price": plan.get("price_raw"),
                "amount": plan.get("amount"),
            })

    for key in old.keys() & new.keys():
        o, n = old[key], new[key]
        if o.get("amount") != n.get("amount") or o.get("currency") != n.get("currency"):
            ev = {
                "type": "price_changed",
                "plan": n.get("name"),
                "from": {"raw": o.get("price_raw"), "amount": o.get("amount"),
                         "currency": o.get("currency")},
                "to": {"raw": n.get("price_raw"), "amount": n.get("amount"),
                       "currency": n.get("currency")},
            }
            try:
                a, b = float(o.get("amount")), float(n.get("amount"))
                if a > 0:
                    ev["pct_change"] = round((b - a) / a * 100, 2)
                ev["direction"] = "up" if b > a else "down"
            except (TypeError, ValueError):
                pass
            events.append(ev)
        elif o.get("period") != n.get("period"):
            events.append({
                "type": "period_changed", "plan": n.get("name"),
                "from": o.get("period"), "to": n.get("period"),
            })

        of, nf = set(o.get("features") or []), set(n.get("features") or [])
        added, removed = sorted(nf - of), sorted(of - nf)
        if added or removed:
            events.append({
                "type": "features_changed",
                "plan": n.get("name"),
                "added": added[:15],
                "removed": removed[:15],
            })

    return events


def text_diff_summary(old_text: str, new_text: str, max_lines: int = 60) -> dict:
    old_lines = (old_text or "").splitlines()
    new_lines = (new_text or "").splitlines()
    added, removed = [], []
    for line in difflib.unified_diff(old_lines, new_lines, n=0, lineterm=""):
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return {
        "lines_added": len(added),
        "lines_removed": len(removed),
        "sample_added": added[:max_lines],
        "sample_removed": removed[:max_lines],
    }


def compare(old: dict | None, new: dict, new_text: str) -> dict | None:
    """None kalau tidak ada perubahan isi."""
    if old is None:
        return {
            "kind": "first_seen",
            "plan_events": [],
            "text": {"lines_added": len(new_text.splitlines()), "lines_removed": 0,
                     "sample_added": [], "sample_removed": []},
        }
    if old.get("content_hash") == new.get("content_hash"):
        return None

    plan_events = diff_plans(old.get("plans") or [], new.get("plans") or [])
    text = text_diff_summary(old.get("_text", ""), new_text)

    # `price_change` DISEDIAKAN KHUSUS untuk angka yang benar-benar bergerak
    # pada paket yang ada di kedua hari. Itu satu-satunya jenis peristiwa yang
    # tidak bisa dipalsukan oleh ekstraksi yang goyah.
    #
    # Penambahan dan penghapusan paket pindah ke `catalog_change`. Alasannya
    # dibuktikan data 02/09: halaman yang dirender JavaScript kadang
    # menampilkan paket dan kadang tidak (synthesia "Free" hilang, suno
    # "Free Plan" muncul), dan baris fitur ikut terbaca sebagai paket
    # ("Claude Code", "Central billing and administration", "Up to 15% off").
    # Selama itu belum beres, add/remove tidak boleh masuk metrik yang
    # menentukan gerbang bulan ke-3.
    #
    # Tidak ada data yang hilang — peristiwanya tetap tercatat lengkap di
    # plan_events, hanya klasifikasinya yang lebih jujur. Kalau ekstraksi
    # sudah stabil, add/remove bisa dinaikkan kembali.
    moved = [e for e in plan_events if e["type"] == "price_changed"]
    catalog = [e for e in plan_events
               if e["type"] in ("plan_added", "plan_removed")]

    if moved:
        kind = "price_change"
    elif catalog:
        kind = "catalog_change"
    elif plan_events:
        kind = "plan_detail_change"
    else:
        kind = "page_change"      # teks berubah tapi harga terdeteksi sama

    return {"kind": kind, "plan_events": plan_events, "text": text}
