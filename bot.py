#!/usr/bin/env python3
"""Sinyal Pagi: crypto picks for a fixed 12-hour window, sent to Discord.

Window   : buy 07:00 WITA, sell 19:00 WITA  (= 23:00 UTC -> 11:00 UTC)
Decision : uses data up to 06:00 WITA (22:00 UTC), so the message is ready before 07:00
Data     : hourly candles from the public Crypto.com Exchange API (no key needed)

  python bot.py morning            # pick coins, send to Discord
  python bot.py evening            # 19:00 result of this morning's picks
  python bot.py morning --dry-run  # print the message instead of sending
  python bot.py test               # send a test message to the webhook

The "perkiraan" (projection) is the historical average 07:00->19:00 return of the
same setup after fees. It is a statistic of the past, not a promise.
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
HOLD_HOURS = 12            # sell 19:00 WITA
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


def window_result(by_ts, decision_end):
    """Return (entry, exit, worst dip) for buying at decision_end+1h and selling 12h later."""
    entry_bar = by_ts.get(decision_end)  # 22:00-23:00 UTC bar, closes at 07:00 WITA
    exit_bar = by_ts.get(decision_end + HOLD_HOURS * HOUR)  # 10:00-11:00 UTC bar, closes 19:00 WITA
    if not entry_bar or not exit_bar:
        return None
    entry, exit_ = entry_bar[4], exit_bar[4]
    lows = [by_ts[t][3] for t in range(decision_end + HOUR, decision_end + (HOLD_HOURS + 1) * HOUR, HOUR) if t in by_ts]
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
    pooled = {k: ([], []) for k in SIGNALS}
    base_pooled = ([], [])
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
        hist = {k: ([], []) for k in SIGNALS}
        base = ([], [])
        for i in range(eng.WARMUP, last):
            w = window_result(by_ts, days[i][0])
            if not w:
                continue
            ret = w[1] / w[0] - 1 - FEE_ROUND_TRIP
            base[0].append(ret)
            base[1].append(w[2])
            for k, s in SIGNALS.items():
                try:
                    hit = s["fn"](d, i)
                except (TypeError, IndexError):
                    hit = False
                if hit:
                    hist[k][0].append(ret)
                    hist[k][1].append(w[2])
        for k in SIGNALS:
            pooled[k][0].extend(hist[k][0])
            pooled[k][1].extend(hist[k][1])
        base_pooled[0].extend(base[0])
        base_pooled[1].extend(base[1])
        active = []
        for k, s in SIGNALS.items():
            try:
                if s["fn"](d, last):
                    active.append(k)
            except (TypeError, IndexError):
                pass
        coins[coin] = {
            "price": hourly[-1][4],
            "chg_24h": days[-1][4] / days[-2][4] - 1,
            "rsi": d["rsi"][last],
            "active": active,
            "hist": {k: summarize(*hist[k]) for k in active},
            "baseline": summarize(*base),
            "recent90": summarize(base[0][-90:], base[1][-90:]),
            "days": len(days),
        }

    pooled_stats = {k: summarize(*pooled[k]) for k in SIGNALS}
    base_stats = summarize(*base_pooled)
    picks = []
    # "Kebiasaan jam": a coin whose own 07->19 window has been positive over the full year
    # AND over the last 90 days. Judged against the all-coin baseline like any other setup.
    for coin, c in coins.items():
        full, recent = c["baseline"], c["recent90"]
        if full["n"] >= 120 and recent["n"] >= 60 and full["avg"] > 0 and recent["avg"] > 0:
            edge = full["win_rate"] - base_stats["win_rate"]
            verdict = "menarik" if edge >= 0.05 and full["win_rate_low"] > 0.5 else "netral"
            picks.append({"coin": coin, "signal": "session", "signal_name": "Kebiasaan jam 07–19 (1 tahun & 90 hari positif)",
                          "source": "coin", "stats": full, "baseline_win": base_stats["win_rate"], "edge": edge,
                          "verdict": verdict, "score": full["win_rate_low"] + max(min(full["avg"], 0.03), -0.03)})
    for coin, c in coins.items():
        for k in c["active"]:
            own = c["hist"][k]
            if own["n"] >= MIN_SAMPLES_COIN:
                st, base, src = own, c["baseline"], "coin"
            elif pooled_stats[k]["n"] >= MIN_SAMPLES_POOLED:
                st, base, src = pooled_stats[k], base_stats, "pooled"
            else:
                continue
            edge = st["win_rate"] - base["win_rate"]
            verdict = ("menarik" if st["avg"] > 0 and edge >= 0.05 and st["win_rate_low"] > 0.5
                       else "lemah" if st["avg"] <= 0 or edge <= 0 else "netral")
            picks.append({"coin": coin, "signal": k, "signal_name": SIGNALS[k]["name"], "source": src,
                          "stats": st, "baseline_win": base["win_rate"], "edge": edge, "verdict": verdict,
                          "score": st["win_rate_low"] + max(min(st["avg"], 0.03), -0.03)})
    best = {}
    for p in sorted(picks, key=lambda p: p["score"], reverse=True):
        best.setdefault(p["coin"], p)
    ranked = sorted(best.values(), key=lambda p: p["score"], reverse=True)
    return {"decision_end": decision_end, "coins": coins, "picks": ranked,
            "pooled": pooled_stats, "baseline": base_stats}


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


def tanggal(dt):
    return f"{HARI[dt.weekday()]} {dt.day} {BULAN[dt.month - 1]}"


def morning_message(res, now_ms):
    trade_day = wita_date(res["decision_end"] + HOUR)  # 07:00 WITA of the trading day
    b = res["baseline"]
    good = [p for p in res["picks"] if p["verdict"] == "menarik"][:MAX_PICKS]
    title = f"Sinyal {tanggal(trade_day)}: beli 07:00, jual 19:00 WITA"
    embeds = []
    if good:
        lines = [f"**{len(good)} koin** yang historisnya lebih baik dari beli acak di jam yang sama."]
        color = 0x1F7A4D
    else:
        lines = ["**Hari ini skip.** Tidak ada setup yang dulu lebih sering untung dari beli acak di jam 07:00–19:00."]
        color = 0x9A6400
    lines.append(f"Patokan beli acak 07→19: untung {pct(b['win_rate'], False)} dari {b['n']:} hari-koin, "
                 f"rata-rata {pct(b['avg'])} setelah biaya {FEE_ROUND_TRIP * 100:.1f}%.")
    embeds.append({"title": title, "description": "\n".join(lines), "color": color})
    for p in good:
        s, c = p["stats"], res["coins"][p["coin"]]
        src = f"riwayat {p['coin']}" if p["source"] == "coin" else "gabungan 23 koin"
        embeds.append({
            "title": f"{p['coin']}  ·  perkiraan {pct(s['avg'])}",
            "color": 0x1D6A86,
            "description": (f"Setup: **{p['signal_name']}**\n"
                            f"Harga acuan sekarang: {usd(c['price'])} · 24 jam {pct(c['chg_24h'])}"),
            "fields": [
                {"name": "Untung di jam main", "value": f"{pct(s['win_rate'], False)} dari {s['n']}× (acak {pct(p['baseline_win'], False)})", "inline": True},
                {"name": "Rentang umum", "value": f"{pct(s['p25'])} s/d {pct(s['p75'])}", "inline": True},
                {"name": "Terburuk", "value": f"{pct(s['worst'])}", "inline": True},
                {"name": "Rata-rata turun terdalam", "value": f"{pct(s['avg_dip'])} sebelum 19:00", "inline": True},
                {"name": "Sumber statistik", "value": src, "inline": True},
            ],
        })
    others = [p for p in res["picks"] if p["verdict"] != "menarik"][:6]
    if others:
        embeds.append({"description": "Muncul tapi historisnya lemah, lebih baik dilewati: " +
                       ", ".join(f"{p['coin']} ({p['signal_name']}, {pct(p['stats']['avg'])})" for p in others),
                       "color": 0x5A6878})
    embeds[-1]["footer"] = {"text": "Statistik masa lalu, bukan janji dan bukan saran keuangan. Pasang batas rugi; ukuran posisi kecil."}
    return {"username": "Sinyal Pagi", "embeds": embeds[:10]}, good


def evening_message(picks_today, results, track):
    lines = []
    for p, r in zip(picks_today, results):
        if r is None:
            lines.append(f"**{p['coin']}**: data jam 19:00 belum tersedia")
            continue
        lines.append(f"**{p['coin']}**: {pct(r['ret'])} (07:00 {usd(r['entry'])} → 19:00 {usd(r['exit'])}), "
                     f"perkiraan pagi {pct(p['projection'])}")
    done = [t for t in track if t.get("ret") is not None]
    tr = ""
    if done:
        wins = sum(t["ret"] > 0 for t in done)
        avg = sum(t["ret"] for t in done) / len(done)
        tr = f"\nRekam jejak: {wins}/{len(done)} untung ({wins / len(done) * 100:.0f}%), rata-rata {pct(avg)} per sinyal setelah biaya."
    return {"username": "Sinyal Pagi", "embeds": [{
        "title": "19:00 WITA: waktunya jual",
        "description": "\n".join(lines) + tr,
        "color": 0x1D6A86,
        "footer": {"text": "Hasil dihitung dari harga Crypto.com, sudah dipotong biaya asumsi. Harga Pluang bisa sedikit beda."},
    }]}


# ---------------- discord ----------------
def send(payload, dry):
    if dry:
        print(json.dumps(payload, indent=1, ensure_ascii=False))
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
        payload, good = morning_message(res, now_ms)
        send(payload, a.dry_run)
        trade_day = wita_date(res["decision_end"] + HOUR).strftime("%Y-%m-%d")
        picks = load_state("picks.json", {})
        picks[trade_day] = [{"coin": p["coin"], "signal_name": p["signal_name"], "projection": p["stats"]["avg"],
                             "win_rate": p["stats"]["win_rate"], "n": p["stats"]["n"],
                             "decision_end": res["decision_end"]} for p in good]
        picks = dict(sorted(picks.items())[-60:])
        if not a.dry_run:
            save_state("picks.json", picks)
        print(f"{len(res['coins'])} coins analysed, {len(res['picks'])} active setups, {len(good)} picks")

    else:  # evening
        trade_day = wita_date(now_ms).strftime("%Y-%m-%d")
        todays = load_state("picks.json", {}).get(trade_day, [])
        if not todays:
            print("No picks this morning; nothing to send.")
            return
        results = []
        for p in todays:
            by_ts = {b[0]: b for b in data.get(p["coin"], [])}
            w = window_result(by_ts, p["decision_end"])
            results.append(None if not w else {"entry": w[0], "exit": w[1], "ret": w[1] / w[0] - 1 - FEE_ROUND_TRIP})
        track = load_state("track.json", [])
        known = {(t["date"], t["coin"]) for t in track}
        for p, r in zip(todays, results):
            if r and (trade_day, p["coin"]) not in known:
                track.append({"date": trade_day, "coin": p["coin"], "signal_name": p["signal_name"],
                              "projection": p["projection"], "ret": r["ret"]})
        send(evening_message(todays, results, track), a.dry_run)
        if not a.dry_run:
            save_state("track.json", track)


if __name__ == "__main__":
    main()
