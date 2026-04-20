# Audit Report — `agentic-trade`

Tanggal: 20 April 2026
Scope: seluruh repo `C:\Users\aruru\agentic-trade` (code, config, bot.log, `data/trades.json`, `backtest_report.html`)

---

## Ringkasan Verdict

**Verdict: Project ini BELUM LAYAK untuk dijalankan sebagai bot profit-konsisten.**

Arsitektur-nya rapi dan fitur-nya lengkap (multi-exchange, regime detector, AI decision layer, risk manager, Telegram alert, backtest engine). Tapi bukti empiris dari *live trading journal*-mu sendiri dan hasil uji ulang saya menunjukkan strategi ini belum punya *edge* yang bisa bertahan setelah dikurangi biaya (fee + slippage). Saat ini ekspektasi matematisnya negatif.

Ringkasan data kunci:

| Sumber | Trades | Win rate | Net PnL | Avg Win | Avg Loss |
|---|---|---|---|---|---|
| `data/trades.json` (live, 14 Apr 2026) | 33 | **36,4%** | **−0,0614 USDT** | +0,0038 | −0,0051 |
| `backtest_report.html` (author's run) | ? | 78,9% | +0,43 (+1,5%) | — | — |
| Backtest-ku pada random-walk (10 seed, no trend) | ~30/run | 57–86% | **+1,98% rata-rata, σ = 10,57%**, cuma **4/10 seed positif** | — | — |

Kesenjangan antara backtest (>75% WR, Sharpe 3+) vs live (36% WR, rugi) adalah *classic overfit / look-ahead-like symptom*.

---

## 1. Arsitektur & Code Quality — Baik

Yang sudah benar:

- Struktur modular: `exchanges/`, `strategy/`, `execution/`, `ai/`, `utils/` — jelas dan bersih.
- Risk manager (`src/execution/risk.py`) sudah cukup matang: position-sizing berbasis ATR, cap max notional $5 USDT, drawdown tracker, exposure check, direction-limit, regime-size modifier.
- Regime detector dua-layer (technical 4H + AI macro Hyperliquid funding/OI) — konsep bagus, ada persistence filter 3× agar tidak flip-flopping.
- Eksekusi SL/TP dikirim satu paket via `place_order_with_sl_tp`.
- Trade journal JSON terpersist; summary feedback loop dimasukkan ke prompt AI.

Kualitas code: layak untuk proyek pribadi / learning, tapi masih banyak *soft spots* untuk production-grade trading (lihat bagian 4).

## 2. Masalah Kritis (Blocker untuk Profit)

### 2.1. Trade size cap terlalu kecil → fee memakan semua edge

`MAX_TRADE_SIZE_USDT=5` (dari `.env`) + leverage default 20× artinya notional efektif ≤ $5. Fee Hyperliquid taker ≈ 0,035% = ~$0,00175 per sisi, ~$0,0035 roundtrip.

Rata-rata kemenangan di journal: **+$0,0038**. Rata-rata kerugian: **−$0,0051**. Artinya, tiap win hanya net ~$0,0003 setelah fee, dan tiap loss masih penuh. Aritmatika edge-nya:

```
EV = 0,364 × 0,0038 + 0,636 × (−0,0051) = −0,0019 USDT/trade
```

Bahkan bila WR naik ke 50%, EV = −0,000065 (breakeven). Strategi harus mencapai WR ≥ ~57% dengan R:R 1:1 saat ini untuk sekadar BEP. Backtest report-mu (28,8 → 29,23 USDT, +$0,43) hanya menghasilkan +1,5% dalam 500 candle — itu pun sebelum slippage realistis.

### 2.2. Backtest meng-overestimate secara drastis

`src/backtest/engine.py`:

- **Eksekusi sempurna di harga SL/TP**. Tidak ada slippage, tidak ada *gap-through* (kalau low < SL, bot mu “dapat” exit tepat di SL — di live bisa lebih jelek terutama pada candle besar).
- **Fee 0,04% flat**, padahal Hyperliquid taker IOC bisa lebih tinggi untuk order tidak tepat di spread. Dan order type-mu adalah IOC dengan aggressive 5% price — pasti taker.
- **Entry di candle close** yang di backtest diasumsikan bisa dieksekusi — di live, harga sudah bergerak sebelum order sampai.
- Trailing stop (line 83–103 engine) memindahkan SL ke breakeven+ begitu profit ≥ 0,5 ATR — tapi di live, trail ini tidak diimplementasikan di `src/main.py`/`execution/`. Jadi **backtest pakai trailing stop, live tidak**. Ini beda-strategi, bukan sekadar beda eksekusi.
- Sumber data default untuk user `backtest` command yang gagal fetch → **synthetic data dengan sinusoidal trend built-in** (`generate_synthetic_data`). Strategi trend-follower tentu “menang” pada data sinus — bukan tes yang valid.

Saya jalankan ulang engine ini di **random walk murni** (tanpa sinus, 10 seed, vol 1,2%):

```
 Seed Trades   Win%     PnL%  MaxDD%
    0     40   75.0   +13.11    4.16
    1     45   68.9    -1.03    9.19
    2     44   86.4   +23.04    5.20
    3     24   75.0    +7.66    6.11
    4     19   84.2   +10.04    3.81
    5     38   71.1    -3.15   11.97
    6     26   61.5    -5.33   10.35
    7     29   62.1   -12.62   13.17
    8     14   57.1    -9.43   10.62
    9     36   66.7    -2.45    7.46
Avg PnL%: +1.98  Std: 10.57  Positive runs: 4/10
```

Empat dari sepuluh seed positif — nyaris coin-flip. Standar deviasi 10× mean. **Strategi ini tidak menunjukkan edge yang signifikan secara statistik.**

### 2.3. Hardcoded model `"glm-5.1"` + `base_url` dari `.env` yang tidak di-wire

`src/ai/agent.py` line 57 dan `src/strategy/regime.py` line 392:

```python
response = self.client.messages.create(model="glm-5.1", ...)
```

`glm-5.1` bukan model Anthropic. Di `.env`-mu ada `LLM_BASE_URL` dan `LLM_MODEL` tapi `config/settings.py` **tidak membaca keduanya**; `anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)` juga **tidak mengoper `base_url`** — hanya berhasil kalau kamu set env var `ANTHROPIC_BASE_URL` secara global di shell. Di log bot memang AI response ke-generate (kamu jelas set env var itu di shell), tapi **konfigurasi ini fragile**: kalau env shell berbeda, bot akan error silently dan fall back ke signal rule-based — yang persis sama dengan signal pre-AI. AI layer kehilangan nilai.

Rekomendasi: pindahkan `LLM_MODEL` dan `LLM_BASE_URL` ke `Settings`, oper ke constructor `anthropic.Anthropic(api_key=..., base_url=...)`, dan jadikan model parameter konfigurasi, bukan string literal di kode.

### 2.4. Sentiment analyzer = keyword counting

`src/strategy/sentiment.py` cuma menghitung kemunculan kata `"surge", "rally", "crash", "dump", …` di result Tavily. Ini:

- **Sangat sensitif terhadap framing berita**: judul “Bitcoin rally stalls as selloff deepens” punya 1 bullish (rally) + 1 bearish (selloff) → hasilnya neutral padahal isi-nya jelas bearish.
- Tidak paham konteks (“no rally in sight” akan dianggap bullish).
- Bobotnya 0,4 (40%) dalam signal combined — cukup besar untuk bisa berbalikkan keputusan.

Di journal trade-mu, beberapa entry SELL dieksekusi karena “bearish sentiment (0,86 confidence)” padahal kondisinya sedang uptrend kuat (ADX 48, EMA stack bullish) — lalu rugi. Ini pola loss yang terulang (6 dari 7 SHORT BTC/ETH di tanggal 14 April rugi).

Rekomendasi: minimal ganti dengan Claude-call untuk klasifikasi sentimen per-artikel (bukan keyword count), atau turunkan bobotnya ke ≤ 0,15, atau jadikan filter saja (block trade kalau sentimen ekstrem) — bukan komponen skor.

### 2.5. Threshold signal rendah untuk live tapi tinggi untuk backtest

- `src/strategy/technical.py`: threshold score = 6 (strict).
- `src/strategy/combined.py`: combined ≥ 0,3 → BUY. Artinya kalau teknikal BUY dengan strength 0,55 saja sudah bisa lolos (0,55 × 0,6 = 0,33).
- `MIN_CONFIDENCE=0,7` di `.env` tapi AI confidence sering dijawab 0,55–0,72 — banyak trade yang lolos margin tipis.
- `min_signal_strength=0,2` default di backtest engine — threshold **jauh lebih longgar** daripada strategi live.

Live menembakkan trade pada signal marginal; backtest saring yang lebih baik. Asimetri ini menyembunyikan losses saat backtest.

### 2.6. Drawdown tracker tidak persist

`RiskManager._peak_balance` di-reset ke 0 setiap kali bot di-restart. Kalau kamu restart setelah drawdown 8%, tracker-nya start dari balance sekarang, seolah tidak ada loss. Max-drawdown safety = tidak andal.

### 2.7. Balance = 0 di log, tapi bot masih jalan 40+ cycle

Di `bot.log` akhirnya semua keputusan AI menolak karena `"Account balance is 0.0 USDT"`. Bot tetap looping tanpa alert. Konsumsi API (Anthropic + Tavily) jalan terus — biaya bot jalan, trade tidak. Perlu early-exit kalau balance < `MAX_TRADE_SIZE_USDT`.

## 3. Masalah Sekunder

- `src/backtest/engine.py` tidak pakai `sentiment` di main loop kecuali sebagai single filter — hanya fetch 1× di awal, lalu pakai state yang sama untuk 500 candle. Tidak realistis.
- Leverage 20× default untuk balance $50 → risiko liquidation amat besar pada wick. 1,5× ATR stop di aset vol tinggi = 3–5% move, cuma butuh 5% adverse untuk wipe margin di 20× jika SL meleset.
- Tidak ada unit test untuk regime detector, sentiment, atau risk manager. `tests/test_strategy.py` ada tapi minimal.
- `TRADING_PAIRS` default BTC/ETH tapi live-nya trading ke PAXG, HYPE, BNB, SOL (berdasarkan `.env` atau override CLI). Universe tidak konsisten dengan asumsi backtest.
- Tidak ada *correlation check*: bot boleh buka BUY BTC + BUY ETH + BUY SOL bersamaan — mereka sangat berkorelasi, jadi "max 2 positions same direction" tidak benar-benar melindungi dari event risk.

## 4. Apakah bisa profit konsisten?

Jawaban jujur: **tidak — belum — dengan konfigurasi & strategi sekarang**. Bukti:

1. **Live journal 33 trade = rugi netto**, WR 36%. Itu dataset kecil tapi arahnya jelas (expected value negatif).
2. **Backtest mu overstated** karena: trailing stop yang tidak ada di live, data sintetik dengan sinus built-in, tidak ada slippage.
3. **Edge statistikal tidak kokoh**: pada random walk seluruh 10 seed, strategi hanya positif di 4 seed, σ >5× mean. Artinya "profit" yang pernah kamu lihat bisa jadi sekadar varian sampling.
4. **Biaya struktural makan habis margin**: fee ≈ 70% dari avg win di size $5.

## 5. Rekomendasi untuk Membuat Project Ini Bisa Profit

Urutan prioritas (actionable):

1. **Perbesar trade size** ke ≥ $50 (10× dari sekarang) atau turunkan leverage; biarkan fee jadi < 10% dari target profit per trade. Kalau modal tidak memungkinkan, jangan live — paper-trade dulu.
2. **Samakan backtest dengan live eksekusi**: implementasikan trailing stop di `src/main.py` *atau* hapus dari backtest. Tambah slippage model (min 5 bps tiap sisi untuk IOC). Tambah fee taker yang benar (~5 bps Hyperliquid).
3. **Uji ulang dengan data pasar nyata minimal 6 bulan**, multi-asset. Jangan pakai `generate_synthetic_data` untuk validasi.
4. **Ganti sentiment keyword counter** dengan LLM classifier atau turunkan bobotnya ke filter boolean (mis. skip trade kalau sentiment extreme opposite + confidence > 0.8).
5. **Perbaiki AI config**: baca `LLM_MODEL` dan `LLM_BASE_URL` dari `settings.py`, oper ke `anthropic.Anthropic(base_url=...)`. Jangan hard-code `"glm-5.1"`.
6. **Persist peak-balance** ke `data/state.json` agar drawdown tracker survive restart.
7. **Early-exit kalau balance < min trade size**. Jangan biarkan bot loop sambil panggil API.
8. **Walk-forward test**: training split (Jan–Jun 2025) → out-of-sample validation (Jul–Dec 2025). Kalau Sharpe OOS < 1,0 — jangan deploy.
9. **Correlation cap**: jangan boleh BUY BTC + BUY ETH + BUY SOL bersamaan. Batas per-cluster.
10. **Tambah unit test** untuk `risk.py` (size, SL/TP math), `regime.py` (deteksi), `fvg.py`. Tanpa test, refactor berisiko.

## 6. Jawaban Singkat untuk Pertanyaan-mu

> Apakah project ini bisa berjalan (profit konsisten)?

**Berjalan: ya** (secara teknis running). **Profit konsisten: tidak, pada konfigurasi ini**. Data live-mu sendiri (33 trade, 36% WR, PnL negatif) adalah bukti paling jelas. Backtest yang terlihat bagus adalah artefak dari sinusoidal test data + trailing stop yang tidak ada di live.

Ada *potensi* kalau kamu:
- menaikkan ukuran trade sehingga fee tidak dominan,
- memperbaiki paritas backtest↔live,
- validasi dengan data pasar nyata 6+ bulan pada walk-forward,
- dan menerima bahwa untuk crypto 1H, edge realistis trend-follower biasanya hanya Sharpe 0,5–1,2 (bukan 3+ seperti angka backtest).

Sebelum semua ini dibereskan: **paper trade dulu, jangan live**.
