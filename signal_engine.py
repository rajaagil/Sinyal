#!/usr/bin/env python3
"""Crypto signal engine — short-term (1-2 day) horizon on daily candles.

Input : JSON file {"BTC": [[ts_ms, open, high, low, close, volume], ...], ...}
        candles sorted oldest -> newest; the LAST candle must be a closed day.
Output: JSON with today's active signals per coin plus historical stats of each
        signal (how often the same setup was profitable after 1 and 2 days).

Nothing here predicts the future. The stats only describe how this setup
behaved in the past, after an assumed round-trip fee.
"""
import json
import math
import sys
from datetime import datetime, timezone

FEE_ROUND_TRIP = 0.004   # 0.4% assumed total cost (buy + sell + spread); adjust to Pluang's real fees
HORIZONS = (1, 2)        # holding period in days
MIN_SAMPLES = 15         # fewer occurrences than this = "data kurang"
WARMUP = 21              # first candle evaluated; each signal also waits until its own indicators exist


# ---------- indicators ----------
def ema(vals, n):
    out, k, e = [None] * len(vals), 2 / (n + 1), None
    for i, v in enumerate(vals):
        if i < n - 1:
            continue
        if e is None:
            e = sum(vals[i - n + 1:i + 1]) / n
        else:
            e = v * k + e * (1 - k)
        out[i] = e
    return out


def rsi(closes, n=14):
    out = [None] * len(closes)
    gains = losses = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l = max(d, 0), max(-d, 0)
        if i <= n:
            gains += g
            losses += l
            if i == n:
                gains /= n
                losses /= n
                out[i] = 100 - 100 / (1 + gains / losses) if losses else 100
        else:
            gains = (gains * (n - 1) + g) / n
            losses = (losses * (n - 1) + l) / n
            out[i] = 100 - 100 / (1 + gains / losses) if losses else 100
    return out


def sma(vals, n):
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        out[i] = sum(vals[i - n + 1:i + 1]) / n
    return out


def stdev(vals, n):
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        w = vals[i - n + 1:i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return out


def indicators(c):
    closes = [x[4] for x in c]
    highs = [x[2] for x in c]
    vols = [x[5] for x in c]
    e9, e21, e50 = ema(closes, 9), ema(closes, 21), ema(closes, 50)
    e12, e26 = ema(closes, 12), ema(closes, 26)
    macd = [a - b if a is not None and b is not None else None for a, b in zip(e12, e26)]
    first = next(i for i, v in enumerate(macd) if v is not None)
    sig_part = ema(macd[first:], 9)
    macd_sig = [None] * first + sig_part
    mid, sd = sma(closes, 20), stdev(closes, 20)
    vol20 = sma(vols, 20)
    hi20 = [None] * len(c)
    for i in range(20, len(c)):
        hi20[i] = max(highs[i - 20:i])  # previous 20 days, excluding today
    return dict(close=closes, rsi=rsi(closes), e9=e9, e21=e21, e50=e50,
                macd=macd, macd_sig=macd_sig, bb_mid=mid, bb_sd=sd,
                vol=vols, vol20=vol20, hi20=hi20)


# ---------- signals (evaluated on candle i, using only data up to i) ----------
def _ok(*xs):
    return all(x is not None for x in xs)


SIGNALS = {
    "rsi_oversold": {
        "name": "RSI oversold memantul",
        "desc": "RSI(14) sempat di bawah 30 kemarin dan hari ini naik lagi. Harga sudah turun dalam, mulai ada pantulan.",
        "fn": lambda d, i: _ok(d["rsi"][i], d["rsi"][i - 1]) and d["rsi"][i - 1] < 30 <= d["rsi"][i],
    },
    "macd_cross": {
        "name": "MACD bullish cross",
        "desc": "Garis MACD memotong ke atas garis sinyal. Momentum jangka pendek berbalik naik.",
        "fn": lambda d, i: _ok(d["macd"][i], d["macd_sig"][i], d["macd"][i - 1], d["macd_sig"][i - 1])
        and d["macd"][i - 1] <= d["macd_sig"][i - 1] and d["macd"][i] > d["macd_sig"][i],
    },
    "ema_cross_trend": {
        "name": "EMA 9/21 cross",
        "desc": "EMA 9 memotong ke atas EMA 21 dan harga tutup di atas keduanya. Tren pendek berbalik naik.",
        "fn": lambda d, i: _ok(d["e9"][i], d["e21"][i], d["e9"][i - 1], d["e21"][i - 1])
        and d["e9"][i - 1] <= d["e21"][i - 1] and d["e9"][i] > d["e21"][i] and d["close"][i] > d["e9"][i],
    },
    "breakout_volume": {
        "name": "Breakout 20 hari + volume",
        "desc": "Harga tutup di atas level tertinggi 20 hari terakhir dengan volume minimal 1,5x rata-rata.",
        "fn": lambda d, i: _ok(d["hi20"][i], d["vol20"][i])
        and d["close"][i] > d["hi20"][i] and d["vol"][i] >= 1.5 * d["vol20"][i],
    },
    "bb_reclaim": {
        "name": "Kembali masuk Bollinger bawah",
        "desc": "Kemarin harga tutup di bawah Bollinger Band bawah, hari ini kembali tutup di atasnya.",
        "fn": lambda d, i: _ok(d["bb_mid"][i], d["bb_sd"][i], d["bb_mid"][i - 1], d["bb_sd"][i - 1])
        and d["close"][i - 1] < d["bb_mid"][i - 1] - 2 * d["bb_sd"][i - 1]
        and d["close"][i] > d["bb_mid"][i] - 2 * d["bb_sd"][i],
    },
}


def near_signals(d, i, active):
    """Setups that are close to triggering. Shown as a watchlist, never ranked."""
    out = []
    r, m, ms = d["rsi"][i], d["macd"][i], d["macd_sig"][i]
    if r is not None and r < 35 and "rsi_oversold" not in active:
        out.append(f"RSI {r:.0f}, mendekati oversold (<30)")
    if _ok(m, ms, d["macd"][i - 1], d["macd_sig"][i - 1]) and m < ms:
        gap_now, gap_prev = ms - m, d["macd_sig"][i - 1] - d["macd"][i - 1]
        if gap_now < gap_prev and gap_now < 0.25 * gap_prev + 1e-12 + abs(ms) * 0.02:
            out.append("MACD hampir cross ke atas")
    hi = d["hi20"][i]
    if hi and d["close"][i] > hi and "breakout_volume" not in active and d["vol20"][i]:
        out.append(f"tembus tertinggi 20 hari, tapi volume baru {d['vol'][i] / d['vol20'][i]:.1f}x rata-rata (perlu 1,5x)")
    elif hi and d["close"][i] <= hi and d["close"][i] >= 0.97 * hi:
        out.append(f"{(hi / d['close'][i] - 1) * 100:.1f}% di bawah tertinggi 20 hari")
    e9, e21 = d["e9"][i], d["e21"][i]
    if _ok(e9, e21, d["e9"][i - 1]) and e9 < e21 and e9 >= 0.99 * e21 and e9 > d["e9"][i - 1]:
        out.append("EMA 9 hampir memotong EMA 21")
    return out


# ---------- stats ----------
def wilson_low(wins, n, z=1.64):
    """Lower bound (~90% one-sided) of the true win rate. Penalises small samples."""
    if n == 0:
        return 0.0
    p = wins / n
    den = 1 + z * z / n
    centre = p + z * z / (2 * n)
    adj = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - adj) / den)


def summarize(rets):
    n = len(rets)
    if n == 0:
        return {"n": 0}
    s = sorted(rets)
    wins = sum(1 for r in rets if r > 0)
    gains = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    return {
        "n": n,
        "win_rate": wins / n,
        "win_rate_low": wilson_low(wins, n),
        "avg": sum(rets) / n,
        "median": s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2,
        "avg_win": sum(gains) / len(gains) if gains else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "worst": s[0],
        "best": s[-1],
    }


def fwd_return(closes, i, h):
    return closes[i + h] / closes[i] - 1 - FEE_ROUND_TRIP


def analyze(data, live_last=False):
    """live_last=True: the last candle of every coin is still forming (used only for
    today's signals, never as an exit price in the backtest)."""
    per_coin = {}
    pooled = {k: {h: [] for h in HORIZONS} for k in SIGNALS}
    base_pooled = {h: [] for h in HORIZONS}

    for coin, candles in data.items():
        if len(candles) < WARMUP + 10:
            continue
        d = indicators(candles)
        cl = d["close"]
        last = len(cl) - 1
        end = last - 1 if live_last else last   # last candle allowed as a backtest exit
        coin_hist = {k: {h: [] for h in HORIZONS} for k in SIGNALS}
        base = {h: [] for h in HORIZONS}
        for i in range(WARMUP, end):
            for h in HORIZONS:
                if i + h <= end:
                    base[h].append(fwd_return(cl, i, h))
            for k, s in SIGNALS.items():
                if s["fn"](d, i):
                    for h in HORIZONS:
                        if i + h <= end:
                            coin_hist[k][h].append(fwd_return(cl, i, h))
        for h in HORIZONS:
            base_pooled[h] += base[h]
            for k in SIGNALS:
                pooled[k][h] += coin_hist[k][h]

        active = [k for k, s in SIGNALS.items() if s["fn"](d, last)]
        near = near_signals(d, last, active)
        chg1 = cl[last] / cl[last - 1] - 1
        chg7 = cl[last] / cl[last - 7] - 1 if last >= 7 else None
        per_coin[coin] = {
            "price": cl[last],
            "date": datetime.fromtimestamp(candles[last][0] / 1000, timezone.utc).strftime("%Y-%m-%d"),
            "chg_1d": chg1,
            "chg_7d": chg7,
            "rsi": d["rsi"][last],
            "above_ema21": cl[last] > d["e21"][last] if d["e21"][last] else None,
            "history_days": len(cl),
            "vol_ratio": d["vol"][last] / d["vol20"][last] if d["vol20"][last] else None,
            "spark": cl[-30:],
            "active": active,
            "near": near,
            "baseline": {str(h): summarize(base[h]) for h in HORIZONS},
            "signal_stats": {k: {str(h): summarize(coin_hist[k][h]) for h in HORIZONS} for k in active},
        }

    pooled_stats = {k: {str(h): summarize(pooled[k][h]) for h in HORIZONS} for k in SIGNALS}
    base_stats = {str(h): summarize(base_pooled[h]) for h in HORIZONS}

    # Rank today's setups. Use the coin's own history when it has enough samples,
    # otherwise the pooled history across all coins. Score = 2-day edge over the
    # "buy any random day" baseline, using the conservative (Wilson) win rate.
    picks = []
    for coin, info in per_coin.items():
        for k in info["active"]:
            own = info["signal_stats"][k]["2"]
            use_own = own["n"] >= MIN_SAMPLES
            st = own if use_own else pooled_stats[k]["2"]
            st1 = info["signal_stats"][k]["1"] if use_own else pooled_stats[k]["1"]
            if st["n"] < MIN_SAMPLES:
                continue
            base = info["baseline"]["2"] if use_own else base_stats["2"]
            edge = st["win_rate"] - base["win_rate"]
            picks.append({
                "coin": coin,
                "signal": k,
                "signal_name": SIGNALS[k]["name"],
                "source": "coin" if use_own else "pooled",
                "stats_2d": st,
                "stats_1d": st1,
                "baseline_2d": {"win_rate": base["win_rate"], "avg": base["avg"]},
                "edge_vs_baseline": edge,
                "score": st["win_rate_low"] + max(min(st["avg"], 0.05), -0.05),
            })
    # a coin can trigger several signals; keep its best one as the headline
    best = {}
    for p in sorted(picks, key=lambda p: p["score"], reverse=True):
        best.setdefault(p["coin"], p)
    ranked = sorted(best.values(), key=lambda p: p["score"], reverse=True)
    for p in ranked:
        p["other_signals"] = [k for k in per_coin[p["coin"]]["active"] if k != p["signal"]]
        p["verdict"] = ("menarik" if p["edge_vs_baseline"] >= 0.05 and p["stats_2d"]["avg"] > 0 and p["stats_2d"]["win_rate_low"] > 0.5
                        else "lemah" if p["stats_2d"]["avg"] <= 0 or p["edge_vs_baseline"] <= 0
                        else "netral")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "candle_date": max((c["date"] for c in per_coin.values()), default=None),
        "fee_assumed": FEE_ROUND_TRIP,
        "min_samples": MIN_SAMPLES,
        "signals": {k: {"name": s["name"], "desc": s["desc"]} for k, s in SIGNALS.items()},
        "pooled_stats": pooled_stats,
        "baseline": base_stats,
        "picks": ranked,
        "coins": per_coin,
    }


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "candles.json"
    dst = sys.argv[2] if len(sys.argv) > 2 else "results.json"
    with open(src) as f:
        data = json.load(f)
    res = analyze(data)
    with open(dst, "w") as f:
        json.dump(res, f, indent=1)
    print(f"{len(res['coins'])} coins, {len(res['picks'])} active setups -> {dst}")
    for p in res["picks"][:5]:
        s = p["stats_2d"]
        print(f"  {p['coin']:6} {p['signal_name']:34} win {s['win_rate']:.0%} (n={s['n']}, {p['source']}) "
              f"avg {s['avg']:+.2%} vs base {p['baseline_2d']['win_rate']:.0%} -> {p['verdict']}")
