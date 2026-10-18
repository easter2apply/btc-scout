"""
Meme Backtest: replay Meme Scout's rules hour by hour on the last ~40 days of the most-traded memecoin pools.

What CAN be replayed from price history: The No-Chase (1h +2..30%, 24h < +150%, 12h+ old), The Volume (24h >= $250K,
last hour >= $10K), the exits (-30% stop, half at +50% then stop to entry, rest at +100%, 72h limit) and ~1.5% costs each way.
What CAN'T (no history): The Crowd (buyers vs sellers), whales, safety. So two versions are reported:
  A) price/volume rules only, every sampled coin
  B) same, but only coins that pass TODAY's safety + holder check (top 10 <= 30%)
Pretend account: $1,000, 10% per buy, max 5 open, in time order.
Big caveat: the sample is coins that are still popular today. Coins that rugged and died are mostly missing,
so these numbers are an optimistic best case.
"""
import json
import statistics as stt
import time

import meme_scout as M
import meme_study as S

COST = 0.015


def signals(cs, created_ts):
    """Hourly candles [ts,o,h,l,c,v] -> list of (index) where the rules say buy."""
    out, last_buy = [], -999
    for i in range(24, len(cs) - 1):
        ts, o, h, l, c, v = cs[i]
        if created_ts and ts + 3600 - created_ts < 12 * 3600:
            continue
        prev = cs[i - 1][4]
        day_ago = cs[i - 24][4]
        if prev <= 0 or day_ago <= 0:
            continue
        h1 = (c / prev - 1) * 100
        h24 = (c / day_ago - 1) * 100
        vol24 = sum(x[5] for x in cs[i - 23:i + 1])
        if 2 <= h1 <= 30 and h24 < 150 and vol24 >= 250000 and v >= 10000 and i - last_buy >= 24:
            out.append(i)
            last_buy = i
    return out


def walk(cs, i):
    """Enter at candle i's close; returns (return on the bet, exit index, how)."""
    entry = cs[i][4] * (1 + COST)
    half = False
    got = 0.0
    for j in range(i + 1, min(len(cs), i + 73)):
        _, o, h, l, c, v = cs[j]
        stop = entry if half else entry * 0.70
        if l <= stop:
            return got + (0.5 if half else 1) * (stop * (1 - COST)) / entry - 1, j, "stop" if not half else "back to entry"
        if h >= entry * 2.0:
            return got + (0.5 if half else 1) * (entry * 2.0 * (1 - COST)) / entry - 1, j, "+100% target"
        if not half and h >= entry * 1.5:
            half = True
            got += 0.5 * (entry * 1.5 * (1 - COST)) / entry
    j = min(len(cs), i + 73) - 1
    if j <= i:
        return None
    return got + (0.5 if half else 1) * (cs[j][4] * (1 - COST)) / entry - 1, j, "time limit" if j == i + 72 else "still open"


def account(trades):
    """$1,000, 10% per buy, max 5 open, in time order."""
    cash, open_, eq_peak, worst = 1000.0, [], 1000.0, 0.0
    for t in sorted(trades, key=lambda t: t["t_in"]):
        for o in [o for o in open_ if o["t_out"] <= t["t_in"]]:
            cash += o["bet"] * (1 + o["ret"]); open_.remove(o)
        val = cash + sum(o["bet"] for o in open_)
        eq_peak = max(eq_peak, val); worst = min(worst, val / eq_peak - 1)
        if len(open_) >= 5:
            continue
        bet = val * 0.10
        if bet > cash:
            continue
        cash -= bet
        open_.append({**t, "bet": bet})
    for o in open_:
        cash += o["bet"] * (1 + o["ret"])
    return round(cash, 2), round(worst * 100, 1)


def summary(trades, days):
    if not trades:
        return {"trades": 0}
    rs = [t["ret"] for t in trades]
    end, dd = account(trades)
    hows = {}
    for t in trades:
        hows[t["how"]] = hows.get(t["how"], 0) + 1
    return {"trades": len(rs), "per_week": round(len(rs) / days * 7, 1), "win_pct": round(sum(1 for r in rs if r > 0) / len(rs) * 100),
            "avg_per_trade_pct": round(stt.mean(rs) * 100, 1), "median_pct": round(stt.median(rs) * 100, 1),
            "best_pct": round(max(rs) * 100), "worst_pct": round(min(rs) * 100),
            "per_100_bet_total": round(sum(rs) * 100), "account_1000_end": end, "account_worst_drop_pct": dd, "exits": hows}


def main():
    t0 = time.time()
    sample = []
    for net in M.NETWORKS:
        try:
            sample += S.pools(net)
        except Exception as e:
            print(net, e)
    seen, uniq = set(), []
    for r in sample:
        if r["token"] not in seen:
            seen.add(r["token"]); uniq.append(r)
    print(len(uniq), "pools", flush=True)
    A, B, first_ts, last_ts = [], [], None, None
    checked = 0
    for k, r in enumerate(uniq):
        if time.time() - t0 > 24 * 60:
            break
        try:
            cs = S.candles(r["net"], r["pool"])
        except Exception as e:
            print(r["symbol"], e); continue
        if len(cs) < 48:
            continue
        first_ts = min(first_ts or cs[0][0], cs[0][0]); last_ts = max(last_ts or cs[-1][0], cs[-1][0])
        created = time.time() - r["age_h"] * 3600 if r["age_h"] else None
        sig = signals(cs, created)
        tr = []
        for i in sig:
            w = walk(cs, i)
            if w and w[2] != "still open":
                tr.append({"coin": r["symbol"], "net": r["net"], "t_in": cs[i][0], "t_out": cs[w[1]][0], "ret": w[0], "how": w[2]})
        A += tr
        if tr and checked < 60:                        # today's safety + holders, for version B
            checked += 1
            try:
                s = M.safety_solana(r["token"]) if r["net"] == "solana" else M.safety_evm(r["net"], r["token"])
            except Exception:
                s = None
            if s:
                hs = sorted([h["pct"] for h in s["holders"]], reverse=True)
                safe = (not s["honeypot"] and s["sell_tax"] <= 10 and not s["mintable"] and not s["freezable"]
                        and (s["lp_locked"] is None or s["lp_locked"] >= 50) and sum(hs[:10]) <= 30)
                if safe:
                    B += tr
        if k % 25 == 0:
            print(k, "coins", len(A), "trades", round((time.time() - t0) / 60, 1), "min", flush=True)
    days = max(1, (last_ts - first_ts) / 86400) if first_ts else 1
    out = {"when": t0, "coins": len(uniq), "days_covered": round(days),
           "A_price_volume_rules": summary(A, days), "B_plus_todays_safety_and_whale_holders": summary(B, days),
           "by_network_A": {n: summary([t for t in A if t["net"] == n], days) for n in M.NETWORKS}}
    json.dump(out, open("meme_backtest.json", "w"), indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
