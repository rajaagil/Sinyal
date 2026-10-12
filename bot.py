#!/usr/bin/env python3
"""Sinyal Pagi: crypto picks for three holding periods, sent to Discord.

Buy 07:00 WITA (23:00 UTC), sell after 12 hours (19:00 WITA), 1 day or 2 days.
Decision : uses data up to 06:00 WITA (22:00 UTC), so the message is ready before 07:00
Data     : hourly candles from the public Crypto.com Exchange API (no key needed)

  python bot.py morning            # pick coins, send to Discord
  python bot.py evening            # 19:00 results of every pick whose holding period ended
  python bot.py morning --dry-run  # print the message instead of sending
  python bot.py test               # send a test message to the webhook

The projection per coin is the historical average return of the same setup over the
same holding period, after fees.
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import signal_engine as eng

# ---------------- settings ----------------
COINS = {  # coin -> Crypto.com instrument (checked available on 12 Oct 2026)
    "BTC": "BTC_USDT", "ETH": "ETH_USDT", "XRP": "XRP_USDT", "SOL": "SOL_USDT", "DOGE": "DOGE_USDT",
    "LINK": "LINK_USDT", "ADA": "ADA_USDT", "HYPE": "HYPE_USD", "NEAR": "NEAR_USDT", "XLM": "XLM_USDT",
    "BCH": "BCH_USDT", "LTC": "LTC_USDT", "AVAX": "AVAX_USDT", "UNI": "UNI_USDT", "SUI": "SUI_USDT",
    "HBAR": "HBAR_USDT", "SHIB": "SHIB_USDT", "ENA": "ENA_USDT", "CRO": "CRO_USDT", "AAVE": "AAVE_USDT",
    "ONDO": "ONDO_USDT", "QNT": "QNT_USDT", "PUMP": "PUMP_USDT",
}
API = "https://api.crypto.com/exchange/v1/public/get-candlestick"
LOOKBACK_DAYS = 365
FEE_ROUND_TRIP = float(os.environ.get("FEE_ROUND_TRIP", "0.004"))  # 0.4% buy+sell+spread
DECISION_HOUR_UTC = 22     # 06:00 WITA: last data used for the decision
ENTRY_HOUR_UTC = 23        # 07:00 WITA
HORIZONS = {               # holding period in hours -> label
    12: "12 jam · jual 19:00 WITA",
    24: "1 hari · jual besok 07:00 WITA",
    48: "2 hari · jual lusa 07:00 WITA",
}
MIN_SAMPLES_COIN = 20      # use a coin's own history once a setup happened this often
MIN_SAMPLES_POOLED = 30
MAX_PICKS = 3
HOUR = 3600 * 1000
DAY = 24 * HOUR
WITA = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
STATE_DIR = os.path.join(HERE, "state")


# ---------------- data ----------------
def http_json(url, tries=4):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sinyal-pagi/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if k == tries - 1:
                raise
            time.sleep(1.5 * (k + 1))
            print(f"  retry {url[-60:]}: {e}", file=sys.stderr)


def fetch_window(inst, start_ms, end_ms):
    q = urllib.parse.urlencode({"instrument_name": inst, "timeframe": "1h", "count": 300,
                                "start_ts": start_ms, "end_ts": end_ms})
    res = http_json(f"{API}?{q}")
    rows = (res.get("result") or {}).get("data") or []
    out = []
    for c in rows:
        t = c.get("t")
        if t is None and c.get("timestamp"):
            t = datetime.fromisoformat(c["timestamp"].replace("Z", "+00:00")).timestamp() * 1000
        out.append([int(t), float(c.get("o", c.get("open"))), float(c.get("h", c.get("high"))),
                    float(c.get("l", c.get("low"))), float(c.get("c", c.get("close"))),
                    float(c.get("v", c.get("volume")))])
    return out


def update_coin(coin, inst, now_ms):
    path = os.path.join(DATA_DIR, f"{coin}.json")
    bars = {}
    if os.path.exists(path):
        with open(path) as f:
            for b in json.load(f):
                bars[b[0]] = b
    start = max(bars) - 6 * HOUR if bars else now_ms - LOOKBACK_DAYS * DAY
    start -= start % HOUR
    while start < now_ms:
        end = min(start + 300 * HOUR, now_ms)
        for b in fetch_window(inst, start, end):
            bars[b[0]] = b
        start = end
        time.sleep(0.12)
    cutoff = now_ms - (LOOKBACK_DAYS + 5) * DAY
    out = sorted((b for t, b in bars.items() if t >= cutoff and t + HOUR <= now_ms), key=lambda b: b[0])
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    return out


def load_all(now_ms, offline=False):
    data = {}
    for coin, inst in COINS.items():
        try:
            if offline:
                with open(os.path.join(DATA_DIR, f"{coin}.json")) as f:
                    data[coin] = [b for b in json.load(f) if b[0] + HOUR <= now_ms]
            else:
                data[coin] = update_coin(coin, inst, now_ms)
            n = len(data[coin])
            print(f"{coin}: {n} hourly bars ({n / 24:.0f} days)")
            if n < 24 * 120:
                print(f"  warning: {coin} has under 120 days of history; its own stats will be thin", file=sys.stderr)
        except Exception as e:  # one coin failing must not kill the report
            print(f"{coin}: FAILED {e}", file=sys.stderr)
    days = {c: round(len(b) / 24) for c, b in data.items()}
    missing = [c for c in COINS if c not in data]
    annotate("Data", f"{len(data)}/{len(COINS)} koin. Hari per koin: " + ", ".join(f"{c} {d}" for c, d in days.items())
             + (f". GAGAL: {', '.join(missing)}" if missing else ""))
    return data


# ---------------- 24h bars anchored at the decision time ----------------
def anchored_days(hourly):
    """24h candles that end at 22:00 UTC (06:00 WITA) each day."""
    groups = {}
    for b in hourly:
        k = (b[0] - DECISION_HOUR_UTC * HOUR) // DAY  # bar k covers [k*DAY+22h, (k+1)*DAY+22h)
        groups.setdefault(k, []).append(b)
    days = []
    for k in sorted(groups):
        g = sorted(groups[k], key=lambda b: b[0])
        if len(g) < 22:  # tolerate up to 2 missing hours
            continue
        end = (k + 1) * DAY + DECISION_HOUR_UTC * HOUR
        days.append([end, g[0][1], max(b[2] for b in g), min(b[3] for b in g), g[-1][4], sum(b[5] for b in g)])
    return days


def window_result(by_ts, decision_end, hours):
    """Buy at 07:00 WITA (decision_end + 1h), sell `hours` later. Returns (entry, exit, worst dip)."""
    entry_bar = by_ts.get(decision_end)                 # bar closing 07:00 WITA
    exit_bar = by_ts.get(decision_end + hours * HOUR)   # bar closing 07:00 + hours
    if not entry_bar or not exit_bar:
        return None
    entry, exit_ = entry_bar[4], exit_bar[4]
    lows = [by_ts[t][3] for t in range(decision_end + HOUR, decision_end + (hours + 1) * HOUR, HOUR) if t in by_ts]
    dip = min(lows) / entry - 1 if lows else None
    return entry, exit_, dip


# ---------------- signals ----------------
SIGNALS = {k: dict(v) for k, v in eng.SIGNALS.items()}
SIGNALS["dip_24h"] = {
    "name": "Turun tajam 24 jam",
    "desc": "Harga turun 4% atau lebih dalam 24 jam terakhir. Diuji apakah biasanya memantul di jam main.",
    "fn": lambda d, i: i >= 1 and d["close"][i] / d["close"][i - 1] - 1 <= -0.04,
}
SIGNALS["momentum_24h"] = {
    "name": "Momentum 24 jam + volume",
    "desc": "Harga naik 4% atau lebih dalam 24 jam dengan volume minimal 1,3x rata-rata 20 hari.",
    "fn": lambda d, i: i >= 1 and d["vol20"][i] is not None and d["close"][i] / d["close"][i - 1] - 1 >= 0.04
    and d["vol"][i] >= 1.3 * d["vol20"][i],
}


def summarize(rets, dips):
    s = eng.summarize(rets)
    if s["n"]:
        srt = sorted(rets)
        s["p25"] = srt[int(0.25 * (len(srt) - 1))]
        s["p75"] = srt[int(0.75 * (len(srt) - 1))]
        dd = [x for x in dips if x is not None]
        s["avg_dip"] = sum(dd) / len(dd) if dd else None
    return s


def analyze(data, now_ms):
    decision_end = (now_ms - DECISION_HOUR_UTC * HOUR) // DAY * DAY + DECISION_HOUR_UTC * HOUR
    H = list(HORIZONS)
    pooled = {h: {k: ([], []) for k in SIGNALS} for h in H}
    coins = {}
    for coin, hourly in data.items():
        if len(hourly) < 24 * 40:
            continue
        by_ts = {b[0]: b for b in hourly}
        days = anchored_days(hourly)
        if not days or days[-1][0] != decision_end:
            print(f"{coin}: no complete 24h bar ending at decision time, skipped", file=sys.stderr)
            continue
        d = eng.indicators(days)
        last = len(days) - 1
        hist = {h: {k: ([], []) for k in SIGNALS} for h in H}
        base = {h: ([], []) for h in H}
        for i in range(eng.WARMUP, last):
            hits = []
            for k, sig in SIGNALS.items():
                try:
                    if sig["fn"](d, i):
                        hits.append(k)
                except (TypeError, IndexError):
                    pass
            for h in H:
                w = window_result(by_ts, days[i][0], h)
                if not w:
                    continue
                ret = w[1] / w[0] - 1 - FEE_ROUND_TRIP
                base[h][0].append(ret)
                base[h][1].append(w[2])
                for k in hits:
                    hist[h][k][0].append(ret)
                    hist[h][k][1].append(w[2])
        for h in H:
            for k in SIGNALS:
                pooled[h][k][0].extend(hist[h][k][0])
                pooled[h][k][1].extend(hist[h][k][1])
        active = []
        for k, sig in SIGNALS.items():
            try:
                if sig["fn"](d, last):
                    active.append(k)
            except (TypeError, IndexError):
                pass
        coins[coin] = {
            "price": hourly[-1][4],
            "chg_24h": days[-1][4] / days[-2][4] - 1,
            "active": active,
            "hist": {h: {k: summarize(*hist[h][k]) for k in active} for h in H},
            "baseline": {h: summarize(*base[h]) for h in H},
            "recent90": {h: summarize(base[h][0][-90:], base[h][1][-90:]) for h in H},
        }

    pooled_stats = {h: {k: summarize(*pooled[h][k]) for k in SIGNALS} for h in H}
    result = {"decision_end": decision_end, "coins": coins, "horizons": {}}
    for h in H:
        cands = []
        for coin, c in coins.items():
            # setups active today, judged on this holding period
            for k in c["active"]:
                own = c["hist"][h][k]
                if own["n"] >= MIN_SAMPLES_COIN:
                    st = own
                elif pooled_stats[h][k]["n"] >= MIN_SAMPLES_POOLED:
                    st = pooled_stats[h][k]
                else:
                    continue
                cands.append((coin, SIGNALS[k]["name"], st, c["baseline"][h]))
            # coin's own habit over this holding period: positive over 1 year AND last 90 days
            full, recent = c["baseline"][h], c["recent90"][h]
            if full["n"] >= 120 and recent["n"] >= 60 and full["avg"] > 0 and recent["avg"] > 0:
                cands.append((coin, "Tren kebiasaan 1 tahun & 90 hari", full, None))
        result.setdefault("debug", {})[h] = sorted(
            [f"{coin} {name}: avg {pct(st['avg'])} win {pct(st['win_rate'], False)} n {st['n']}"
             + (f" acak {pct(base['win_rate'], False)}" if base else "") for coin, name, st, base in cands],
            key=lambda x: -float(x.split("avg ")[1].split("%")[0]))[:8]
        picks = []
        for coin, name, st, base in cands:
            better_than_random = base is None or st["win_rate"] > base["win_rate"]
            if st["avg"] >= 0.001 and st["win_rate"] >= 0.5 and better_than_random:
                picks.append({"coin": coin, "signal_name": name, "stats": st})
        best = {}
        for p in sorted(picks, key=lambda p: p["stats"]["avg"], reverse=True):
            best.setdefault(p["coin"], p)
        result["horizons"][h] = sorted(best.values(), key=lambda p: p["stats"]["avg"], reverse=True)[:MAX_PICKS]
    return result


# ---------------- formatting ----------------
def pct(x, signed=True, digits=1):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{x * 100:+.{digits}f}%" if signed else f"{x * 100:.0f}%"


def usd(p):
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 1:
        return f"${p:,.3f}"
    return f"${p:.{max(4, 2 - int(math.floor(math.log10(p))) + 2)}f}"


def wita_date(ms):
    return datetime.fromtimestamp(ms / 1000, WITA)


HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
BULAN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]


def tgl_pendek(iso_day):
    d = datetime.fromisoformat(iso_day)
    return f"{d.day} {BULAN[d.month - 1]}"


def tanggal(dt):
    return f"{HARI[dt.weekday()]} {dt.day} {BULAN[dt.month - 1]}"


def morning_message(res):
    trade_day = wita_date(res["decision_end"] + HOUR)
    embeds = [{"title": f"Sinyal {tanggal(trade_day)} · beli 07:00 WITA", "color": 0x142231}]
    colors = {12: 0x1D6A86, 24: 0x1F7A4D, 48: 0x9A6400}
    for h, label in HORIZONS.items():
        picks = res["horizons"][h]
        if picks:
            lines = []
            for i, p in enumerate(picks, 1):
                s = p["stats"]
                lines.append(f"**{i}. {p['coin']}**  ·  **{pct(s['avg'])}**  ·  peluang naik {pct(s['win_rate'], False)}"
                             f"  ·  terburuk {pct(s['worst'])}\n{p['signal_name']}")
            desc = "\n\n".join(lines)
        else:
            desc = "Tidak ada koin yang layak."
        embeds.append({"title": label, "description": desc, "color": colors[h]})
    return {"username": "Sinyal Pagi", "embeds": embeds}


def evening_message(rows):
    by_h = {}
    for r in rows:
        by_h.setdefault(r["hours"], []).append(r)
    embeds = [{"title": "Hasil sinyal", "color": 0x142231}]
    for h, label in HORIZONS.items():
        if h not in by_h:
            continue
        lines = [f"**{r['coin']}**  ·  **{pct(r['ret'])}**  (perkiraan {pct(r['projection'])}, beli {tgl_pendek(r['date'])})" for r in by_h[h]]
        embeds.append({"title": label.split(" · ")[0], "description": "\n".join(lines), "color": 0x1D6A86})
    return {"username": "Sinyal Pagi", "embeds": embeds}


# ---------------- discord ----------------
def annotate(title, text):
    """Short note shown on the GitHub Actions run page (readable without opening the log)."""
    if os.environ.get("GITHUB_ACTIONS"):
        msg = text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print(f"::notice title={title}::{msg}")


def send(payload, dry):
    if dry:
        print(json.dumps(payload, indent=1, ensure_ascii=False))
        lines = []
        for e in payload.get("embeds", []):
            if e.get("title"):
                lines.append("# " + e["title"])
            if e.get("description"):
                lines.append(e["description"])
            for f in e.get("fields", []):
                lines.append(f"- {f['name']}: {f['value']}")
        annotate("Pesan (dry run)", "\n".join(lines) or payload.get("content", ""))
        return
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url.startswith("https://"):
        sys.exit("DISCORD_WEBHOOK_URL belum diisi (Settings > Secrets and variables > Actions).")
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "sinyal-pagi/1.0"})
    for k in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                print(f"Discord: HTTP {r.status}")
                return
        except urllib.error.HTTPError as e:
            if e.code == 429 and k < 2:
                time.sleep(3)
                continue
            sys.exit(f"Discord menolak pesan: HTTP {e.code} {e.read()[:300]!r}")


# ---------------- state ----------------
def load_state(name, default):
    p = os.path.join(STATE_DIR, name)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return default


def save_state(name, obj):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, name), "w") as f:
        json.dump(obj, f, indent=1)


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["morning", "evening", "test"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--offline", action="store_true", help="use data/ as is, no download")
    ap.add_argument("--now", help="override current time, ISO UTC (testing)")
    a = ap.parse_args()
    now = datetime.fromisoformat(a.now).replace(tzinfo=timezone.utc) if a.now else datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    if a.mode == "test":
        send({"username": "Sinyal Pagi", "content": "Tes koneksi berhasil. Laporan pertama datang besok 06:30–07:00 WITA."}, a.dry_run)
        return

    data = load_all(now_ms, a.offline)

    if a.mode == "morning":
        res = analyze(data, now_ms)
        send(morning_message(res), a.dry_run)
        if a.dry_run:
            annotate("Kandidat sebelum filter", "\n".join(f"[{h} jam] " + (" | ".join(v) or "tidak ada setup aktif")
                                                           for h, v in res.get("debug", {}).items()))
        trade_day = wita_date(res["decision_end"] + HOUR).strftime("%Y-%m-%d")
        picks = load_state("picks.json", {})
        picks[trade_day] = {str(h): [{"coin": p["coin"], "signal_name": p["signal_name"],
                                      "projection": p["stats"]["avg"], "decision_end": res["decision_end"]}
                                     for p in ps] for h, ps in res["horizons"].items()}
        picks = dict(sorted(picks.items())[-60:])
        if not a.dry_run:
            save_state("picks.json", picks)
        print(f"{len(res['coins'])} coins analysed; picks per horizon: "
              + ", ".join(f"{h}h {len(ps)}" for h, ps in res["horizons"].items()))

    else:  # evening: report every pick whose holding period has ended and was not reported yet
        picks = load_state("picks.json", {})
        track = load_state("track.json", [])
        done = {(t["date"], t.get("hours", 12), t["coin"]) for t in track}
        rows = []
        for day, per_h in picks.items():
            if isinstance(per_h, list):  # old format (12h only)
                per_h = {"12": per_h}
            for hs, ps in per_h.items():
                h = int(hs)
                for p in ps:
                    if (day, h, p["coin"]) in done or p["decision_end"] + (h + 1) * HOUR > now_ms:
                        continue
                    by_ts = {b[0]: b for b in data.get(p["coin"], [])}
                    w = window_result(by_ts, p["decision_end"], h)
                    if not w:
                        continue
                    r = {"date": day, "hours": h, "coin": p["coin"], "signal_name": p["signal_name"],
                         "projection": p["projection"], "ret": w[1] / w[0] - 1 - FEE_ROUND_TRIP}
                    rows.append(r)
                    track.append(r)
        if not rows:
            print("Nothing matured; no message.")
            return
        send(evening_message(rows), a.dry_run)
        if not a.dry_run:
            save_state("track.json", track[-1000:])


if __name__ == "__main__":
    main()
