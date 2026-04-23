# Live Test Playbook — $40 · 1 Month · Hyperliquid Perps

Tujuan: validasi seluruh stack (tech + sentiment + alpha + risk) dengan modal
kecil sebelum scaling. $40 dipilih karena cukup besar untuk perps minimum order
($10) tapi cukup kecil supaya total loss maksimum pun nggak sakit.

---

## Pre-flight (lakukan sekali sebelum start)

### 1. Fund Hyperliquid account

Transfer **$45 USDC** ke main wallet (ekstra $5 untuk fees + funding). Pakai
Arbitrum bridge atau deposit native dari CEX yang support Hyperliquid.

### 2. Set leverage per asset di Hyperliquid UI

- BTC: **3x**
- ETH: **3x**
- HYPE: **2x** (lebih volatile)

Lower leverage = lebih banyak room kalau stop kehit (margin call lebih jauh).
Don't use > 5x sampai 3 bulan track record solid.

### 3. API wallet

Hyperliquid pakai wallet-based auth. Di `app.hyperliquid.xyz/API`:

- Buat API wallet baru (separate dari main wallet)
- Copy private key → `HYPERLIQUID_API_KEY`
- Copy API wallet address → `WALLET_ADDRESS`
- Main wallet address → `HYPERLIQUID_ACCOUNT_ADDRESS`

API wallet hanya bisa trade — nggak bisa withdraw. Aman kalau key bocor.

### 4. Telegram bot

1. Chat `@BotFather` di Telegram → `/newbot` → pilih nama → catat token
2. Kirim message pertama ke bot lo (biar chat_id kebentuk)
3. Buka `https://api.telegram.org/bot<TOKEN>/getUpdates` → ambil `chat.id`
4. Isi `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` di `.env`

### 5. Copy config

```
cd C:\Users\aruru\agentic-trade
copy .env .env.backup
copy .env.livetest .env
```

Lalu buka `.env` dan isi key yang masih kosong (Hyperliquid, Anthropic, Tavily,
Telegram).

### 6. Smoke test — dry run 1 cycle

```
venv\Scripts\activate
python -m src.main run --exchange hyperliquid --timeframe 1h --once
```

Expected output (normal):
- `Starting bot on hyperliquid with pairs: ['BTC/USDT', 'ETH/USDT', 'HYPE/USDT']`
- Per-symbol cycle dengan signal + AI decision
- Telegram dapet startup message
- Exit code 0

Kalau error di fetch_balance → cek Hyperliquid keys. Kalau error di LLM → cek
Anthropic key. Jangan lanjut ke Task Scheduler sebelum `--once` jalan bersih
minimal 2x berturut-turut.

---

## Scheduling: Daemon vs Task Scheduler

Dua cara run bot terus-menerus. Pilih salah satu:

### Opsi A — Daemon dengan auto-restart (recommended)

Satu proses persistent, loop internal tiap 5 menit. Wrapper batch
auto-restart kalau crash. Lebih efisien (WebSocket nggak reconnect tiap cycle).

```
run-live-daemon.bat
```

Atau minimize background:
```
start "agentic-trade" /min run-live-daemon.bat
```

Auto-start saat Windows login (opsional, buat resilient terhadap reboot):
1. Task Scheduler → **Create Task** → name `agentic-trade-daemon`
2. **Triggers:** At log on
3. **Actions:** Program `C:\Users\aruru\agentic-trade\run-live-daemon.bat`,
   Start in `C:\Users\aruru\agentic-trade`
4. **Conditions:** uncheck "Start only if AC"
5. **Settings:** uncheck "Stop task if runs longer than..." (daemon persistent)

Log di `data\daemon.log`. Stop: Ctrl-C di window, atau close window.

**Caveat:** kalau MAX_DRAWDOWN trip → process exit → daemon restart 30s kemudian,
tapi akan exit lagi karena peak_balance belum reset. Kalau kena kill-switch:
stop daemon → reset state (lihat Troubleshooting) → start lagi.

### Opsi B — Task Scheduler polling (lebih robust, sedikit overhead)

Fresh process tiap 5 menit. Tahan crash, tahan reboot, zero memory leak.
Cocok kalau komputer sering sleep/wake.

1. **Task Scheduler** (Win + R → `taskschd.msc`)
2. **Create Task** → **General:** name `agentic-trade-cycle`, ☑ Run with highest privileges
3. **Triggers:** Daily, recur 1 day, ☑ Repeat every **5 minutes** for **1 day**
4. **Actions:** Program `C:\Users\aruru\agentic-trade\run-live.bat`,
   Start in `C:\Users\aruru\agentic-trade`
5. **Conditions:** ☐ Start only if AC, ☑ Wake computer to run
6. **Settings:** ☑ Run on demand, "Do not start new instance" kalau masih jalan,
   Stop if runs > **4 minutes**

Test: right-click task → Run. Cek `data\runner.log` ter-append
`=== cycle start/end (exit 0) ===`.

---

## Monitoring protocol

### Harian (5 menit per hari)

1. Cek Telegram — ada alerts besar?
2. Buka `data\trades.json` → hitung jumlah trades hari ini, P&L
3. Tail `bot.log` — ada WARNING/ERROR yang aneh?
4. Cek Hyperliquid UI — posisi + margin ratio sehat?

### Weekly (30 menit setiap Minggu)

Cara paling cepat — export journal ke xlsx:

```
python -m src.main export --output data/weekly-YYYYMMDD.xlsx
# atau filter 7 hari terakhir saja:
python -m src.main export --days 7 --output data/week.xlsx
```

File xlsx berisi 4 sheet:
- **Trades** — satu baris per trade (entry/exit/PnL/reasoning), row hijau/merah sesuai outcome.
- **Summary** — total closed, WR, profit factor, net, best/worst, notional (semua formula).
- **By Symbol** — WR + Net PnL per pair.
- **Daily P&L** — UTC-day buckets + kolom cumulative (equity curve).

Buka di Excel/LibreOffice — formula auto-recalculate saat file dibuka. Isi
angka ke template tracking:

| Week | Start Bal | End Bal | Trades | Wins | Losses | WR% | Gross PnL | Fees | Net PnL | MaxDD% | Notes |
|------|-----------|---------|--------|------|--------|-----|-----------|------|---------|--------|-------|
| 1    | $40.00    |         |        |      |        |     |           |      |         |        |       |

Quick-check di terminal (kalau males buka xlsx):

```python
import json
trades = [t for t in json.load(open("data/trades.json")) if t.get("status") == "closed"]
pnls = [t["pnl"] for t in trades]
wins = sum(1 for p in pnls if p > 0)
print(f"Trades: {len(trades)} | WR: {wins/len(trades):.1%} | Net: ${sum(pnls):.2f}")
```

### Kill-switches yang udah aktif

| Switch | Trigger | Effect |
|--------|---------|--------|
| MAX_DRAWDOWN (15%) | Peak→trough $6 loss | **Process exit** (manual restart) |
| DAILY_LOSS_KILL (6%) | Intraday $2.40 loss | Pause new entries 24h, trailing masih jalan |
| MIN_TRADE_SIZE gate | Balance < $10 | Skip cycle, sleep 5 min |
| Cluster cap (1) | 1 pos di L1_MAJOR | Block kedua BTC/ETH trade |
| Funding skip (60% annual) | Extreme funding | Skip entry, log reason |
| AI veto (conf ≥ 0.80) | AI HOLD/opposite at high conf | Reject trade, log `ai_veto_hold/opposite` |
| Tavily circuit (1800/2000) | Monthly cap near | Skip news fetch, sentiment falls back to keyword |
| Econ event blackout (T-30m) | High-impact USD event ≤ 30 min away | Reject new entries, log `event_blackout` |
| Econ event size-cut (±2h) | High-impact USD event ±2h window | Notional × 0.5 (open trades still managed) |

### Rejection telemetry (Phase 1-6 output)

Setiap cycle sekarang log kenapa trade di-reject — nggak ada lagi silent
"hold → 0 orders". Cek `bot.log` untuk pola:

```
[BTC/USDC] REJECT=signal_hold — combined=hold tech=buy(str=0.42) sent=bullish regime=sideways
[ETH/USDC] REJECT=low_confidence — signal_conf=0.55 < min=0.72
[HYPE/USDC] REJECT=ai_veto_hold — AI HOLD at conf=0.83 >= veto_threshold=0.80
=== Cycle summary: executed=1 rejected=2 pairs=3 ===
  Rejection breakdown: ai_veto_hold=1, low_confidence=1
```

Rejection reasons yang mungkin muncul:
- `signal_hold` — combined blend tidak tradable (too weak direction)
- `low_confidence` — combined conf < MIN_CONFIDENCE
- `ai_veto_hold` / `ai_veto_opposite` — AI override at high conf
- `risk_blocked` — pre-trade risk check (exposure, cluster cap, etc.)
- `sizing_zero` — sizing modifiers collapsed notional to 0
- `funding_skip` — funding extreme adverse
- `event_blackout` — high-impact USD macro event within T-30 min (Phase 9)
- `order_exception` / `order_failed` — exchange rejected order

### Telegram verbosity knobs (Phase 6)

`TELEGRAM_DIGEST_INTERVAL_CYCLES=12` → rolling digest tiap ~1 jam berisi
executed/rejected counts, rejection breakdown, Tavily budget remaining.
Set 0 untuk mute digest, still dapat trade/close alerts.

`TELEGRAM_HOLD_ALERT_ENABLED=false` → kalau `true`, tiap rejected symbol
dapet own Telegram message. Noisy banget — hanya flip ON 1-2 hari saat
lagi tuning MIN_CONFIDENCE/AI_VETO_MIN_CONFIDENCE, lalu off lagi.

### Economic calendar awareness (Phase 9)

Bot sekarang sadar FOMC / CPI / NFP / PCE / PPI / GDP / Retail Sales / ISM.
Sekali per 24 jam narik `ff_calendar_thisweek.json` dari ForexFactory
(gratis, no key) dan cache ke `data/econ_calendar.json`.

Dua efek pada $40 live-test:

1. **Hard blackout T-30 menit** — pas ada event USD HIGH dalam 30 menit
   ke depan, semua entry baru di-reject dengan reason `event_blackout`.
   Trailing stop / SL / TP posisi yang udah open **tetap jalan**; bot
   cuma nggak buka baru di window yang spread-nya lagi 5-10× normal.
2. **Size modifier ±2 jam** — dalam ±2 jam window dari event yang sama,
   notional dikali `ECON_EVENT_SIZE_MULT` (default 0.5). Jadi misal
   30 menit setelah CPI baru keluar, tech signal kuat masuk — bot boleh
   trade, tapi size-nya setengah. Whipsaw post-print nggak akan ngabisin
   akun kecil.

Firecrawl fallback tersedia kalau direct HTTP ke faireconomy di-block
(ISP / corp firewall). Isi `FIRECRAWL_API_KEY` di `.env` — <1 credit/hari
di refresh 24h, basically free dari kuota 500/bulan.

Knob ringkas:
- `ECON_CALENDAR_ENABLED=true` — master switch
- `ECON_BLACKOUT_MIN=30` — blackout window (menit sebelum event)
- `ECON_EVENT_WINDOW_H=2.0` — size-modifier window (±jam)
- `ECON_EVENT_SIZE_MULT=0.5` — multiplier dalam window
- `ECON_TRACK_CURRENCIES=USD` — kalau mau tambah EUR, bikin comma-list
- `ECON_TRACK_IMPACT=High` — bisa ditambah Medium kalau mau paranoid
- `ECON_EVENT_WARN_AHEAD_H=2.0` — Telegram heads-up T-2 jam per event

Telegram akan kirim **📅 Macro Event Incoming** saat event matching masuk
`ECON_EVENT_WARN_AHEAD_H` jam ke depan (dedup per-boot; restart untuk
re-arm warning yang sama). Cek juga log `bot.log`:

```
Econ calendar loaded: 28 events (source=faireconomy_json)
Econ event ahead: USD FOMC Rate Decision (High) — in 1.6h
[BTC/USDC] REJECT=event_blackout — USD FOMC Rate Decision @ 2026-04-23T18:00:00+00:00
[ETH/USDC] event sizing x0.50 (USD Core CPI m/m)
```

Kalau pengen disable sepenuhnya (e.g. lagi backtest kondisi equilibrium):
set `ECON_CALENDAR_ENABLED=false` — semua kode calendar di-skip, gate
balik ke Phase 1-6 behavior.

### Manual abort criteria

Stop test dan review kalau:
- **3 hari berturut-turut merah** (daily P&L negative 3 days in a row)
- **7 losing trades beruntun** tanpa winner
- **Balance < $32** (−20% off-target, walaupun belum kena MaxDD)
- **≥3 unhandled crash per hari** di runner.log
- **Fees > 50% gross PnL** setelah 2 minggu (strategi kalah sama cost)

---

## Week-by-week expectations

### Week 1 — Infrastructure validation
Target: **0 crash, ≥50% signals dieksekusi, MaxDD ≤ 5%**. Strategi bakal
over-trade atau skip terlalu banyak — itu normal. Focus: **tidak ada unhandled
exception**.

Kalau ada 1-2 crash, identify root cause (biasanya API rate limit atau
Hyperliquid WS disconnect) — fix, lanjut.

### Week 2 — Behavior observation
Target: **WR ≥ 40%, net P&L ≥ −5%**. Masih boleh negatif; yang penting bot
behaviour konsisten sama backtest. Cek:
- Alpha engine sering HOLD atau sering fire?
- Funding filter skip berapa banyak trade?
- Sentiment bias sering agree sama tech atau bertabrakan?

Log decision rationale (`bot.log` punya AI reasoning tiap cycle) — cari
pattern.

### Week 3 — Tuning window
Boleh adjust **satu knob** berdasarkan Week 1-2 data:
- Kalau terlalu banyak skip → turunin MIN_CONFIDENCE dari 0.72 ke 0.65
- Kalau terlalu sering trip daily-loss → turunin DAILY_LOSS_KILL dari 6% ke 5%
- Kalau alpha terlalu dominant (banyak trade low-quality) → turunin ALPHA_WEIGHT dari 0.25 ke 0.15

**Satu knob. Satu minggu**. Jangan multi-variable tuning di test kecil ini.

### Week 4 — Decision
Target: **Net P&L > 0, MaxDD < 10%, WR > 45%, PF > 1.2**. Kalau hit:
- Scale ke $100 dengan same config
- Add 1-2 pair non-L1_MAJOR (PAXG counter-cycle, atau minor L1)
- Extend test 1 bulan lagi

Kalau miss:
- Audit trade journal per-category (long vs short, per-symbol, per-regime)
- Identify mana yang bocor (biasanya: counter-trend trades di bull regime,
  atau holding terlalu lama)
- Patch-and-retest, jangan scale

---

## Troubleshooting

### Bot nggak start
```
cd C:\Users\aruru\agentic-trade
venv\Scripts\activate
python -m src.main run --once
```
Error di console, fix dulu sebelum balikin ke Task Scheduler.

### Task Scheduler run tapi exit non-zero
Tail `data\runner.log` — last 100 lines. Paling sering:
- Venv activation fail → install venv di path yang bener
- HL API key expired → regenerate
- Network flap → biasanya self-heal di cycle berikutnya

### Daily-loss lock nyangkut
Kalau lock belum expire tapi lo yakin mau override:
```
python -c "import json; s=json.load(open('data/state.json')); s['lock_until_ts']=0; json.dump(s,open('data/state.json','w'),indent=2)"
```
Hati-hati — lock ada alasannya.

### Reset peak_balance
Kalau MaxDD kehit karena artifact (e.g. temporary balance fetch bug):
```
python -c "import json; s=json.load(open('data/state.json')); s['peak_balance']=0; json.dump(s,open('data/state.json','w'),indent=2)"
```

---

## Success metrics (after 30 days)

Minimum to declare "viable":
- Final balance ≥ $40 (break-even after fees)
- Sharpe-like ratio: `net_pnl / max_drawdown > 2.0`
- No unexplained behaviour in log
- Kill-switches triggered ≤ 2x (shows risk kicks in but doesn't dominate)

Stretch goal:
- Final balance ≥ $48 (+20% in 1 month)
- WR ≥ 50%, PF ≥ 1.5
- Fees < 30% of gross PnL

Anything below minimum = go back to backtest, audit, iterate. Don't scale.
