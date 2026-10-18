"""
Meme Study: WHEN do memecoins move, and HOW LONG after launch do they pop?

Sample: the most-traded pools right now on Solana, Base and BNB Chain (GeckoTerminal), hourly candles.
  1) Clock: share of trading volume and average move for each hour of the day (Eastern time) and each weekday
  2) Launch timing (coins launched in the last ~40 days, so we see their whole life):
     - hours from launch to their all-time high
     - if you bought at the end of hour 1 / hour 6 / day 1: how often it doubled, how long that took, where it is now
     - when the single biggest pump candle happened
Note: these are coins that are popular TODAY, so the sample leans toward winners (survivorship bias).
"""
import datetime as dt
import json
import statistics as stt
import time

import meme_scout as M

ET = dt.timezone(dt.timedelta(hours=-4))
PAGES = 5
SKIP = {"SOL", "WSOL", "WETH", "ETH", "USDC", "USDT", "WBNB", "BNB", "CBBTC", "WBTC", "BTCB", "USD1", "DAI", "FDUSD", "JUP", "JITOSOL", "MSOL"}


def med(xs):
    return round(stt.median(xs), 2) if xs else None


def pct(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 2) if xs else None


def pools(net):
    """Most-traded pools + trending pools (24h and 6h) for one network."""
    out, raw = [], []
    calls = [("pools", {"page": p, "sort": "h24_volume_usd_desc", "include": "base_token"}) for p in range(1, PAGES + 1)]
    calls += [("trending_pools", {"page": p, "duration": d, "include": "base_token"}) for d in ("24h", "6h") for p in range(1, 4)]
    for path, params in calls:
        j = M.get(f"{M.GT}/networks/{net}/{path}", params, gt=True)
        if j is None and "sort" in params:
            params = {k: v for k, v in params.items() if k != "sort"}
            j = M.get(f"{M.GT}/networks/{net}/{path}", params, gt=True)
        j = j or {}
        print(net, path, params.get("page"), params.get("duration", ""), len(j.get("data", [])), flush=True)
        toks = {t["id"]: t["attributes"] for t in j.get("included", []) if t.get("type") == "token"}
        for p in j.get("data", []):
            a = p["attributes"]
            tid = ((p.get("relationships") or {}).get("base_token") or {}).get("data", {}).get("id", "")
            raw.append(M.pool_row(net, a, toks.get(tid, {}), tid))
    for r in raw:
        if (r["symbol"] or "").upper() in SKIP or r["fdv"] > 2e9 or r["liq"] < 20000:
            continue
        out.append(r)
    return out


def candles(net, pool):
    j = M.get(f"{M.GT}/networks/{net}/pools/{pool}/ohlcv/hour", {"aggregate": 1, "limit": 1000, "currency": "usd"}, gt=True) or {}
    rows = ((j.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    rows = sorted([[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in rows if r[1] and r[4]])
    return rows


def main():
    t0 = time.time()
    sample = []
    for net in M.NETWORKS:
        try:
            sample += pools(net)
        except Exception as e:
            print(net, e)
    seen, uniq = set(), []
    for r in sample:
        if r["token"] not in seen:
            seen.add(r["token"]); uniq.append(r)
    print(len(uniq), "pools", flush=True)
    hour_vol, hour_abs, hour_ret = {}, {}, {}
    wd_vol, wd_abs = {}, {}
    launch = {"ath_hours": [], "biggest_pump_hour": [], "biggest_pump_pct": [], "now_vs_ath": [], "peak_x_from_h1": []}
    entries = {1: [], 6: [], 24: []}
    n_launch = 0
    for k, r in enumerate(uniq):
        if time.time() - t0 > 22 * 60:
            break
        try:
            cs = candles(r["net"], r["pool"])
        except Exception as e:
            print(r["symbol"], e); continue
        if len(cs) < 24:
            continue
        tot_v = sum(c[5] for c in cs) or 1
        for ts, o, h, l, c, v in cs:
            e = dt.datetime.fromtimestamp(ts, ET)
            w = v / tot_v                                      # each coin counts equally
            hour_vol[e.hour] = hour_vol.get(e.hour, 0) + w
            wd_vol[e.strftime("%a")] = wd_vol.get(e.strftime("%a"), 0) + w
            if o > 0:
                hour_abs.setdefault(e.hour, []).append(abs(c / o - 1))
                hour_ret.setdefault(e.hour, []).append(c / o - 1)
                wd_abs.setdefault(e.strftime("%a"), []).append(abs(c / o - 1))
        # launch analysis: only if the candles start near the pool's creation
        if r["age_h"] is not None and r["age_h"] <= 960 and len(cs) >= 48 and (cs[0][0] - (time.time() - r["age_h"] * 3600)) < 3 * 3600:
            n_launch += 1
            highs = [c[2] for c in cs]
            ath_i = max(range(len(cs)), key=lambda i: highs[i])
            launch["ath_hours"].append(ath_i)
            pumps = [(c[2] / c[1] - 1, i) for i, c in enumerate(cs) if c[1] > 0]
            if pumps:
                pp, pi = max(pumps)
                launch["biggest_pump_hour"].append(pi); launch["biggest_pump_pct"].append(pp * 100)
            launch["now_vs_ath"].append((cs[-1][4] / highs[ath_i] - 1) * 100)
            for hrs in entries:
                if len(cs) > hrs + 24:
                    entry = cs[hrs - 1][4]
                    after = cs[hrs:]
                    peak = max(c[2] for c in after)
                    t2 = next((i + 1 for i, c in enumerate(after) if c[2] >= 2 * entry), None)
                    halved = next((i + 1 for i, c in enumerate(after) if c[3] <= 0.5 * entry), None)
                    entries[hrs].append({"peak_x": peak / entry, "t2x": t2, "halved_first": halved is not None and (t2 is None or halved < t2),
                                         "now_x": cs[-1][4] / entry})
        if k % 25 == 0:
            print(k, "coins", round((time.time() - t0) / 60, 1), "min", flush=True)

    tot = sum(hour_vol.values()) or 1
    clock = {f"{h % 12 or 12}{'am' if h < 12 else 'pm'}": {"volume_share_pct": round(hour_vol.get(h, 0) / tot * 100, 2),
                                                          "avg_move_pct": round(stt.mean(hour_abs[h]) * 100, 2) if hour_abs.get(h) else None,
                                                          "avg_direction_pct": round(stt.mean(hour_ret[h]) * 100, 3) if hour_ret.get(h) else None}
             for h in range(24)}
    wt = sum(wd_vol.values()) or 1
    week = {d: {"volume_share_pct": round(wd_vol.get(d, 0) / wt * 100, 1),
                "avg_hourly_move_pct": round(stt.mean(wd_abs[d]) * 100, 2) if wd_abs.get(d) else None}
            for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")}
    ent = {}
    for hrs, xs in entries.items():
        if not xs:
            continue
        dbl = [x for x in xs if x["t2x"]]
        ent[f"bought after {hrs}h"] = {
            "coins": len(xs), "doubled_pct": round(len(dbl) / len(xs) * 100), "median_hours_to_2x": med([x["t2x"] for x in dbl]),
            "fell_50pct_before_doubling_pct": round(sum(1 for x in xs if x["halved_first"]) / len(xs) * 100),
            "median_peak_x": med([x["peak_x"] for x in xs]), "median_now_x": med([x["now_x"] for x in xs]),
            "now_up_pct": round(sum(1 for x in xs if x["now_x"] > 1) / len(xs) * 100),
            "now_down_80pct_plus": round(sum(1 for x in xs if x["now_x"] < 0.2) / len(xs) * 100)}
    L = {"coins_with_full_history": n_launch, "median_hours_launch_to_ath": med(launch["ath_hours"]),
         "ath_within_24h_pct": round(sum(1 for h in launch["ath_hours"] if h <= 24) / max(1, n_launch) * 100),
         "ath_within_72h_pct": round(sum(1 for h in launch["ath_hours"] if h <= 72) / max(1, n_launch) * 100),
         "median_hour_of_biggest_pump": med(launch["biggest_pump_hour"]), "median_biggest_1h_pump_pct": med(launch["biggest_pump_pct"]),
         "median_now_below_ath_pct": med(launch["now_vs_ath"]), "p25_now_below_ath_pct": pct(launch["now_vs_ath"], 0.25)}
    out = {"when": t0, "pools": len(uniq), "clock_et": clock, "weekday": week, "launch": L, "entries": ent}
    json.dump(out, open("meme_study.json", "w"), indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
