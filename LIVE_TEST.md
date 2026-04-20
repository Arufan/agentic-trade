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

## Schedule via Windows Task Scheduler

### Setup task

1. Buka **Task Scheduler** (Win + R → `taskschd.msc`)
2. **Create Task** (bukan Create Basic Task)
3. **General tab:**
   - Name: `agentic-trade-cycle`
   - ☑ Run whether user is logged on or not
   - ☑ Run with highest privileges
4. **Triggers tab:** New →
   - Daily, recur every 1 day
   - ☑ Repeat every **5 minutes** for duration of **1 day**
   - ☑ Enabled
5. **Actions tab:** New →
   - Action: Start a program
   - Program/script: `C:\Users\aruru\agentic-trade\run-live.bat`
   - Start in: `C:\Users\aruru\agentic-trade`
6. **Conditions tab:**
   - ☐ Start only if AC (uncheck — biar tetap jalan di battery)
   - ☑ Wake the computer to run this task (kalau komputer sering sleep)
7. **Settings tab:**
   - ☑ Allow task to be run on demand
   - If running, do not start a new instance
   - Stop task if runs longer than **4 minutes** (kalau stuck, kill sebelum next cycle)

### Test task manually

Right-click task → Run. Expected:
- `data\runner.log` ter-append dengan `=== cycle start ===` dan `=== cycle end (exit 0) ===`
- Telegram dapet notifikasi (kalau ada signal)
- `data\trades.json` update kalau ada trade

Kalau exit code ≠ 0 → buka `data\runner.log` tail 200 lines, debug dulu.

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
