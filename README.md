# ai-pricing-tracker

Arsip harian harga tool AI, developer, dan SaaS produktivitas.

Yang jadi aset bukan harga hari ini — siapa pun bisa membuka halaman harga
Cursor. Yang tidak dimiliki siapa pun adalah **riwayatnya**: kapan berubah,
dari berapa ke berapa, fitur apa yang diam-diam pindah tier. Data itu hanya
ada kalau dikumpulkan sejak hari pertama.

Riwayat git repo ini **adalah** arsipnya. Setiap commit harian adalah snapshot
bertanggal yang bisa diverifikasi siapa pun.

---

## Status

Fase 1 (bulan 1–3): **mengumpulkan, belum menerbitkan apa pun.**

- [x] Pengumpul data + normalisasi + hash stabil
- [x] Ekstraktor hybrid (adapter → JSON-LD → heuristik DOM)
- [x] Pembanding + log perubahan append-only
- [x] GitHub Actions harian
- [ ] Validasi 48 target terhadap situs sungguhan (`validate-targets`)
- [ ] Naikkan ke ~200 target
- [ ] Situs statis (bulan ke-3)

---

## Cara pakai

```bash
pip install -r requirements.txt

python -m tests.test_pipeline            # uji pipeline, tidak menyentuh internet
python -m scripts.check_targets          # cek daftar target ke situs sungguhan
python -m collector.run --limit 5 --dry-run
python -m collector.run                  # jalan penuh (sekali per hari)
python -m collector.run --only cursor,vercel --force
python scripts/summarize_run.py --markdown
```

Penjaga sekali-per-hari aktif secara otomatis: target yang sudah berhasil
diambil hari ini akan dilewati kecuali kamu memakai `--force`.

---

## Tata letak data

```
data/current/<slug>.json    keadaan terkini: hash, paket, tabel harga
data/current/<slug>.txt     teks halaman ternormalisasi  <- `git diff` di sini
                            langsung menunjukkan apa yang berubah
data/raw/<slug>/<tgl>.html.gz   HTML bersih, HANYA ditulis saat isi berubah
data/changes/changes.jsonl      log perubahan append-only
data/runs/<tgl>.json            log eksekusi harian per target
```

Berkas `current/` sengaja **tidak memuat timestamp**. Kalau ada, setiap commit
harian akan menghasilkan diff palsu dan riwayat git jadi tidak terbaca.
Waktu pengambilan hidup di `data/runs/`.

Contoh satu baris `changes.jsonl`:

```json
{"date":"2026-08-29","slug":"acme","kind":"price_change",
 "plan_events":[{"type":"price_changed","plan":"Pro",
 "from":{"raw":"$20","amount":20.0},"to":{"raw":"$25","amount":25.0},
 "pct_change":25.0,"direction":"up"}]}
```

---

## Etika dan batas pengambilan data

Ini bukan formalitas. Kalau proyek ini dianggap scraper agresif, asetnya mati
sebelum sempat tumbuh. Aturan berikut ditegakkan di kode, bukan cuma di dokumen:

| Aturan | Ditegakkan di |
| --- | --- |
| `robots.txt` dihormati; yang melarang dilewati | `fetcher.fetch` |
| robots.txt 5xx/gagal → **tidak** diambil (RFC 9309) | `RobotsCache._load` |
| `Crawl-delay` dipatuhi, minimum 6 detik per host | `HostGate` |
| Maksimum satu permintaan per halaman per hari | `run.main_async` |
| User-Agent menyebut nama proyek + URL kontak | `config.USER_AGENT` |
| Hormati `Retry-After` saat kena 429 | `fetcher.fetch` |
| Tolak halaman > 6 MB | `config.MAX_BYTES` |

Yang **tidak** ditegakkan kode dan jadi tanggung jawabmu saat menambah target:

- Jangan pernah menambah URL di balik login atau paywall.
- Baca ketentuan layanan situsnya. Yang melarang, jangan dimasukkan.
- Utamakan sumber resmi yang memang dimaksudkan dibaca mesin.
- Situs yang menerbitkan hasilnya nanti **wajib** menautkan balik ke halaman
  harga resmi setiap tool.

**Sebelum menjalankan produksi:** ganti `APT_CONTACT_URL` (default masih
`github.com/CHANGEME/...`). Bot tanpa alamat kontak yang benar adalah bot yang
pantas diblokir.

---

## Menambah target

1. Tambahkan entri di `targets/targets.yaml`.
2. Jalankan workflow **validate-targets** di GitHub Actions (atau
   `python -m scripts.check_targets --only <slug>` dari mesin dengan akses
   internet penuh).
3. Baca kolom catatan:
   - `robots_denied` → **buang targetnya**, jangan diakali.
   - `tidak ada harga di HTML awal` → coba `render: js`, atau cari halaman
     harga alternatif yang statis.
   - `cuma 1 paket terdeteksi` → periksa manual; mungkin butuh adapter.
4. Kalau lolos, commit.

`slug` **tidak boleh diubah** setelah dipakai — mengubahnya memutus riwayat
arsip untuk tool itu. Untuk menonaktifkan target, pakai `enabled: false`,
jangan dihapus.

---

## Menulis adapter

Heuristik generik sengaja dibuat konservatif. Kalau satu situs hasilnya buruk,
tulis adapter — **setelah melihat HTML aslinya**, jangan menebak.

```python
# collector/adapters/cursor.py
from ..extract import Plan, parse_period, parse_price


def extract(soup, raw_html) -> list[Plan]:
    plans = []
    for card in soup.select("[data-testid='pricing-card']"):
        name = card.select_one("h3").get_text(strip=True)
        text = card.get_text(" ", strip=True)
        amount, currency, raw = parse_price(text)
        plans.append(Plan(name=name, price_raw=raw, amount=amount,
                          currency=currency, period=parse_period(text)))
    return plans          # kembalikan [] kalau ragu -> jatuh ke generik
```

Nama berkas = slug dengan `-` diganti `_`. Adapter yang melempar exception
dicatat sebagai error lalu pipeline lanjut memakai ekstraktor generik. **Arsip
tidak pernah berhenti karena adapter patah** — itu aturan desainnya.

Kandidat adapter muncul otomatis di ringkasan run harian, di bagian
"Ekstraksi lemah".

---

## Kenapa hash-nya harus stabil

Halaman harga modern menyisipkan nonce, id acak, dan build hash yang berubah
tiap muat. Kalau hash dihitung dari HTML mentah, setiap hari akan tercatat
sebagai "perubahan" dan arsipnya jadi sampah.

`collector/normalize.py` membuang script, style, svg, banner cookie, lalu
mengganti UUID/hex panjang/timestamp dengan placeholder, dan menghitung hash
dari **teks yang terlihat** saja. Test `tests/test_pipeline.py` memverifikasi
ini: halaman yang sama dengan nonce berbeda **harus** menghasilkan hash
identik, dan perubahan harga sungguhan **harus** mengubahnya.

Kalau kamu menyentuh `normalize.py`, jalankan ulang test itu.

---

## Titik keputusan

- **Bulan 3** — minimal 100 perubahan harga tercatat? Kalau tidak, himpunan
  datanya terlalu statis; ganti target, jangan tambah fitur.
- **Bulan 6** — ada pengunjung yang kembali tanpa disuruh?
- **Bulan 12** — sudah $500/bulan?

Cek progres kapan saja:

```bash
wc -l data/changes/changes.jsonl
python -c "
import json,collections
k=collections.Counter()
for l in open('data/changes/changes.jsonl'):
    k[json.loads(l)['kind']]+=1
print(k)
"
```

---

## Lisensi

Kode: MIT. Data di `data/`: CC BY 4.0 — silakan dipakai dengan atribusi.
Menjaga datanya terbuka justru yang membuatnya jadi rujukan; monetisasinya
lewat tautan afiliasi dan penjualan akses terstruktur, bukan lewat mengunci
angkanya.
