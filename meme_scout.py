"""
Meme Scout — a PAPER-ONLY memecoin scanner (the coins you'd see on apps like fomo).

Every run it looks at the trending coins on Solana, Base and BNB Chain, then five members check each one:
  The Inspector     safety: can you actually sell (honeypot / sell tax), can the dev print more or freeze you,
                    is the money behind it (liquidity) real and locked
  The Whale Watcher how much the biggest wallets hold, and whether whales ($5K+ trades) bought or sold in the last 24h
  The Volume        real trading volume, not too thin and not suspiciously high for the money behind it
  The Crowd         more buyers than sellers in the last hour
  The No-Chase      moving up, but not already flying (no buying a coin that's already +150% today)
All five YES = a pretend buy with the pretend $1,000 account. No real money, no wallet, no orders.
Data: GeckoTerminal (prices, volume, whale trades), RugCheck (Solana safety + holders), GoPlus (Base/BNB safety + holders).
Writes site/meme.html and meme_state.json. Telegram alert on each pretend buy/sell (set ALERTS = False to stop).
"""
import datetime as dt
import html
import json
import os
import time
import urllib.parse
import urllib.request

START = 1000.0
BET_PCT = 0.10              # each pretend buy = 10% of the pretend account
MAX_OPEN = 5
STOP = -0.30                # sell everything at -30%
HALF_AT = 0.50              # sell half at +50%, then the rest can't lose (stop to entry)
TARGET = 1.00               # sell the rest at +100%
MAX_HOLD_H = 72
COSTS = 0.015               # ~0.5% app fee + ~1% slippage, each way
ALERTS = True
WHALE_USD = 5000
NETWORKS = {"solana": "Solana", "base": "Base", "bsc": "BNB Chain"}
GOPLUS_CHAIN = {"base": "8453", "bsc": "56"}
STATE = "meme_state.json"
TZ = dt.timezone(dt.timedelta(hours=-4))
GT = "https://api.geckoterminal.com/api/v2"
MEMBERS = ["The Inspector", "The Whale Watcher", "The Volume", "The Crowd", "The No-Chase"]
MAX_CHECKS = 12             # deep checks per run (free APIs have rate limits)
RECHECK_MIN = 60            # don't deep-check the same coin more than once an hour

_last_gt = [0.0]


def get(url, params=None, gt=False, tries=3):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for k in range(tries):
        if gt:                                  # GeckoTerminal free tier: ~30 calls/min
            wait = 2.2 - (time.time() - _last_gt[0])
            if wait > 0:
                time.sleep(wait)
            _last_gt[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "meme-scout/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and k < tries - 1:
                time.sleep(8); continue
            if e.code in (400, 404):
                return None
            if k == tries - 1:
                raise
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(2)


def f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def money(x):
    a = abs(x)
    s = f"${a / 1e9:.2f}B" if a >= 1e9 else f"${a / 1e6:.2f}M" if a >= 1e6 else f"${a / 1e3:.1f}K" if a >= 1e3 else f"${a:,.2f}"
    return "-" + s if x < 0 else s


def price_fmt(p):
    return f"${p:.2e}" if p < 0.0001 else f"${p:.6f}" if p < 1 else f"${p:,.4f}"


# ───────────────────────── data ─────────────────────────
def trending(net):
    j = get(f"{GT}/networks/{net}/trending_pools", {"include": "base_token", "page": 1}, gt=True) or {}
    toks = {t["id"]: t["attributes"] for t in j.get("included", []) if t.get("type") == "token"}
    out = []
    for p in j.get("data", []):
        a = p["attributes"]
        tid = ((p.get("relationships") or {}).get("base_token") or {}).get("data", {}).get("id", "")
        t = toks.get(tid, {})
        out.append(pool_row(net, a, t, tid))
    return out


def pool_row(net, a, t, tid):
    tx = a.get("transactions") or {}
    pc = a.get("price_change_percentage") or {}
    vol = a.get("volume_usd") or {}
    created = a.get("pool_created_at")
    age_h = None
    if created:
        try:
            age_h = (time.time() - dt.datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()) / 3600
        except ValueError:
            pass
    return {"net": net, "pool": a.get("address"), "token": t.get("address") or tid.split("_", 1)[-1], "symbol": t.get("symbol") or a.get("name", "?").split(" /")[0],
            "name": t.get("name") or a.get("name"), "price": f(a.get("base_token_price_usd")), "liq": f(a.get("reserve_in_usd")),
            "mcap": f(a.get("market_cap_usd")) or f(a.get("fdv_usd")), "fdv": f(a.get("fdv_usd")), "age_h": age_h,
            "ch": {k: f(pc.get(k)) for k in ("m5", "h1", "h6", "h24")}, "vol": {k: f(vol.get(k)) for k in ("m5", "h1", "h6", "h24")},
            "tx_h1": tx.get("h1") or {}, "tx_h24": tx.get("h24") or {}}


def whale_trades(net, pool):
    j = get(f"{GT}/networks/{net}/pools/{pool}/trades", {"trade_volume_in_usd_greater_than": WHALE_USD}, gt=True) or {}
    buys = sells = 0.0
    nb = ns = 0
    wallets = set()
    cut = time.time() - 86400
    for t in j.get("data", []):
        a = t["attributes"]
        try:
            ts = dt.datetime.fromisoformat(a["block_timestamp"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        if ts < cut:
            continue
        v = f(a.get("volume_in_usd"))
        if a.get("kind") == "buy":
            buys += v; nb += 1; wallets.add(a.get("tx_from_address"))
        else:
            sells += v; ns += 1
    return {"buy_usd": buys, "sell_usd": sells, "buys": nb, "sells": ns, "buyers": len(wallets)}


def safety_solana(mint):
    r = get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
    if not r:
        return None
    known = r.get("knownAccounts") or {}
    holders = []
    for h in r.get("topHolders") or []:
        tag = (known.get(h.get("owner")) or known.get(h.get("address")) or {}).get("type", "")
        if tag in ("AMM", "LOCKER", "BURN") or h.get("owner") in known and "pool" in str(known[h["owner"]]).lower():
            continue                                              # pool / locked / burned: not a whale
        holders.append({"pct": f(h.get("pct")), "insider": bool(h.get("insider"))})
    lp_locked = max([f((m.get("lp") or {}).get("lpLockedPct")) for m in r.get("markets") or []] or [0])
    risks = [(x.get("name", ""), x.get("level", "")) for x in r.get("risks") or []]
    return {"holders": holders, "honeypot": False, "sell_tax": 0.0, "mintable": bool(r.get("mintAuthority")),
            "freezable": bool(r.get("freezeAuthority")), "lp_locked": lp_locked,
            "danger": [n for n, lvl in risks if lvl == "danger"], "warn": [n for n, lvl in risks if lvl == "warn"],
            "holder_count": r.get("totalHolders")}


def safety_evm(net, addr):
    j = get(f"https://api.gopluslabs.io/api/v1/token_security/{GOPLUS_CHAIN[net]}", {"contract_addresses": addr}) or {}
    r = (j.get("result") or {}).get(addr.lower())
    if not r:
        return None
    holders = [{"pct": f(h.get("percent")) * 100, "insider": False} for h in r.get("holders") or []
               if not h.get("is_locked") and str(h.get("is_contract")) != "1"
               and h.get("address", "").lower() not in ("0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000")]
    lp_locked = sum(f(h.get("percent")) for h in r.get("lp_holders") or [] if str(h.get("is_locked")) == "1"
                    or h.get("address", "").lower().endswith("dead") or h.get("address", "").lower() == "0x0000000000000000000000000000000000000000") * 100
    danger = []
    if r.get("is_honeypot") == "1": danger.append("honeypot (you can't sell)")
    if r.get("cannot_sell_all") == "1": danger.append("can't sell all your tokens")
    if r.get("owner_change_balance") == "1": danger.append("owner can change balances")
    if r.get("hidden_owner") == "1": danger.append("hidden owner")
    if r.get("transfer_pausable") == "1": danger.append("trading can be paused")
    if r.get("is_open_source") == "0": danger.append("code not public")
    return {"holders": holders, "honeypot": r.get("is_honeypot") == "1", "sell_tax": f(r.get("sell_tax")) * 100,
            "mintable": r.get("is_mintable") == "1", "freezable": False, "lp_locked": min(lp_locked, 100.0),
            "danger": danger, "warn": [], "holder_count": r.get("holder_count")}


# ───────────────────────── the council ─────────────────────────
def vote(c, s, w):
    v = {}
    # The Inspector
    bad = []
    if s is None:
        v["The Inspector"] = (False, "couldn't get a safety report, so no")
    else:
        if s["honeypot"]: bad.append("honeypot: you can't sell")
        if s["sell_tax"] > 10: bad.append(f"sell tax {s['sell_tax']:.0f}%")
        if s["mintable"]: bad.append("dev can print more coins")
        if s["freezable"]: bad.append("dev can freeze your coins")
        if s["lp_locked"] < 50: bad.append(f"only {s['lp_locked']:.0f}% of the liquidity is locked (rug risk)")
        bad += [d for d in s["danger"] if d not in bad][:3]
        if c["liq"] < 75000: bad.append(f"thin liquidity ({money(c['liq'])})")
        v["The Inspector"] = (not bad, "; ".join(bad) if bad else
                              f"sellable, no mint/freeze, {s['lp_locked']:.0f}% liquidity locked, {money(c['liq'])} liquidity")
    # The Whale Watcher
    if s is None:
        v["The Whale Watcher"] = (False, "no holder data")
    else:
        hs = sorted([h["pct"] for h in s["holders"]], reverse=True)
        top10, top1 = sum(hs[:10]), (hs[0] if hs else 0)
        ins = sum(1 for h in s["holders"][:10] if h["insider"])
        net = w["buy_usd"] - w["sell_usd"]
        ok = top10 <= 30 and top1 <= 8 and net >= 0 and ins == 0
        why = (f"top 10 wallets hold {top10:.0f}% (biggest {top1:.1f}%)" + (f", {ins} insider wallets" if ins else "")
               + f"; whales ($5K+) bought {money(w['buy_usd'])} ({w['buys']}) vs sold {money(w['sell_usd'])} ({w['sells']}) in 24h")
        if top10 > 30: why += " → too concentrated: a few wallets can dump on you"
        elif net < 0: why += " → whales are selling"
        v["The Whale Watcher"] = (ok, why)
    # The Volume
    vr = c["vol"]["h24"] / c["liq"] if c["liq"] else 0
    okv = c["vol"]["h24"] >= 250000 and c["vol"]["h1"] >= 10000 and 0.5 <= vr <= 25
    v["The Volume"] = (okv, f"{money(c['vol']['h24'])} traded in 24h, {money(c['vol']['h1'])} last hour ({vr:.1f}× its liquidity)"
                       + ("" if okv else " → " + ("too thin" if c['vol']['h24'] < 250000 or c['vol']['h1'] < 10000 else
                                                  "suspiciously high for the money behind it (fake volume?)" if vr > 25 else "barely trading")))
    # The Crowd
    t = c["tx_h1"]
    b, sl, byr, slr = t.get("buys", 0), t.get("sells", 0), t.get("buyers", 0), t.get("sellers", 0)
    okc = b > sl and byr > slr and byr >= 20
    v["The Crowd"] = (okc, f"last hour: {byr} buyers vs {slr} sellers ({b} buys / {sl} sells)")
    # The No-Chase
    h1, h24 = c["ch"]["h1"], c["ch"]["h24"]
    age = c["age_h"] or 0
    okn = 2 <= h1 <= 30 and h24 < 150 and age >= 12
    why = f"{h1:+.0f}% this hour, {h24:+.0f}% today, {age:.0f}h old"
    if age < 12: why += " → too new (most rugs happen in the first hours)"
    elif h24 >= 150: why += " → already flew, too late"
    elif h1 > 30: why += " → going vertical, don't chase"
    elif h1 < 2: why += " → not moving up"
    v["The No-Chase"] = (okn, why)
    return v


# ───────────────────────── paper trading ─────────────────────────
def load():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {"cash": START, "open": [], "closed": [], "feed": [], "checked": {}, "bought": {}, "runs": 0, "equity": []}


def tg(st, text):
    if not ALERTS:
        return
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    cid = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not cid:
        try:
            cid = str(json.load(open("btc_state.json")).get("chat_id") or "")
        except (OSError, ValueError):
            cid = ""
    if not (tok and cid):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": cid, "text": text}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=15)
    except Exception as e:
        print("telegram:", e)


def value(st):
    return st["cash"] + sum(p["qty"] * p["last"] * (1 - COSTS) for p in st["open"])


def sell(st, p, frac, why, now):
    qty = p["qty"] * frac
    got = qty * p["last"] * (1 - COSTS)
    st["cash"] += got
    p["qty"] -= qty
    p["realized"] = p.get("realized", 0) + got
    if frac >= 0.999 or p["qty"] <= 1e-12:
        pnl = p["realized"] - p["cost"]
        st["open"].remove(p)
        st["closed"].append({**p, "exit_t": now, "why": why, "pnl": round(pnl, 2), "pct": round(pnl / p["cost"] * 100, 1)})
        tg(st, f"🧪 Meme Scout (paper)\nSOLD {p['symbol']} ({NETWORKS[p['net']]}): {why}\nResult: {money(pnl)} ({pnl / p['cost'] * 100:+.0f}%)")
    else:
        p.setdefault("events", []).append(f"sold half: {why}")


def manage(st, now):
    if not st["open"]:
        return
    by_net = {}
    for p in st["open"]:
        by_net.setdefault(p["net"], []).append(p)
    for net, ps in by_net.items():
        addrs = ",".join(p["pool"] for p in ps)[:2000]
        j = get(f"{GT}/networks/{net}/pools/multi/{addrs}", gt=True) or {}
        px = {d["attributes"]["address"]: f(d["attributes"].get("base_token_price_usd")) for d in j.get("data", [])}
        for p in list(ps):
            if px.get(p["pool"]):
                p["last"] = px[p["pool"]]
            ch = p["last"] / p["entry"] - 1
            p["peak"] = max(p.get("peak", 0), ch)
            age = (now - p["t"]) / 3600
            if ch <= (0 if p.get("half") else STOP):
                sell(st, p, 1, "stop hit" if not p.get("half") else "back to entry after taking half", now)
            elif ch >= TARGET:
                sell(st, p, 1, f"target +{TARGET:.0%}", now)
            elif ch >= HALF_AT and not p.get("half"):
                p["half"] = True
                sell(st, p, 0.5, f"+{HALF_AT:.0%}", now)
            elif age >= MAX_HOLD_H:
                sell(st, p, 1, f"{MAX_HOLD_H}h time limit", now)


def run(st, log):
    now = time.time()
    manage(st, now)
    cands = []
    for net in NETWORKS:
        try:
            cands += trending(net)
        except Exception as e:
            log.append(f"{net}: {e}")
    st["last_scan"] = [{"symbol": c["symbol"], "net": c["net"], "price": c["price"], "liq": c["liq"], "vol24": c["vol"]["h24"],
                        "h1": c["ch"]["h1"], "h24": c["ch"]["h24"], "mcap": c["mcap"]} for c in cands]
    # cheap pre-screen before the slow deep checks
    pre = [c for c in cands if c["liq"] >= 30000 and c["vol"]["h24"] >= 100000 and c["price"] > 0
           and now - st["checked"].get(c["token"], 0) >= RECHECK_MIN * 60]
    pre.sort(key=lambda c: -c["vol"]["h1"])
    st["checks_total"] = st.get("checks_total", 0) + len(pre[:MAX_CHECKS])
    for c in pre[:MAX_CHECKS]:
        try:
            s = safety_solana(c["token"]) if c["net"] == "solana" else safety_evm(c["net"], c["token"])
        except Exception as e:
            s = None; log.append(f"safety {c['symbol']}: {e}")
        try:
            w = whale_trades(c["net"], c["pool"])
        except Exception as e:
            w = {"buy_usd": 0, "sell_usd": 0, "buys": 0, "sells": 0, "buyers": 0}; log.append(f"whales {c['symbol']}: {e}")
        st["checked"][c["token"]] = now
        v = vote(c, s, w)
        ok = all(x[0] for x in v.values())
        hs = sorted([h["pct"] for h in (s or {}).get("holders", [])], reverse=True)
        card = {"t": now, "symbol": c["symbol"], "name": c["name"], "net": c["net"], "token": c["token"], "pool": c["pool"],
                "price": c["price"], "mcap": c["mcap"], "liq": c["liq"], "vol24": c["vol"]["h24"], "vol1": c["vol"]["h1"],
                "h1": c["ch"]["h1"], "h24": c["ch"]["h24"], "top10": round(sum(hs[:10]), 1) if s else None,
                "holders": (s or {}).get("holder_count"), "whales": w, "votes": {k: [a, b] for k, (a, b) in v.items()}, "ok": ok}
        if ok:
            if len(st["open"]) >= MAX_OPEN:
                card["ok"] = False; card["note"] = f"{MAX_OPEN} pretend trades already open"
            elif any(p["token"] == c["token"] for p in st["open"]) or now - st["bought"].get(c["token"], 0) < 86400:
                card["ok"] = False; card["note"] = "already bought this one in the last day"
        if card["ok"]:
            bet = round(value(st) * BET_PCT, 2)
            if bet > st["cash"]:
                card["ok"] = False; card["note"] = "not enough pretend cash"
            else:
                st["cash"] -= bet
                entry = c["price"]
                st["open"].append({"t": now, "symbol": c["symbol"], "name": c["name"], "net": c["net"], "token": c["token"], "pool": c["pool"],
                                   "entry": entry, "last": entry, "qty": bet * (1 - COSTS) / entry, "cost": bet, "votes": card["votes"]})
                st["bought"][c["token"]] = now
                tg(st, f"🧪 Meme Scout (paper)\nPRETEND BUY {c['symbol']} on {NETWORKS[c['net']]} at {price_fmt(entry)}\n"
                       f"{money(bet)} · top 10 wallets {card['top10']}% · whales net {money(w['buy_usd'] - w['sell_usd'])} 24h\n"
                       f"Not advice: paper test only.")
        st["feed"].append(card)
    st["feed"] = st["feed"][-150:]
    st["checked"] = {k: v for k, v in st["checked"].items() if now - v < 86400}
    st["bought"] = {k: v for k, v in st["bought"].items() if now - v < 7 * 86400}
    st["equity"].append([now, round(value(st), 2)])
    st["equity"] = st["equity"][-2000:]
    log.append(f"{len(cands)} trending coins, {min(len(pre), MAX_CHECKS)} deep-checked, {len(st['open'])} open")


# ───────────────────────── page ─────────────────────────
def page(st):
    e = html.escape
    val = value(st)
    closed = st["closed"]
    wins = sum(1 for p in closed if p["pnl"] > 0)
    pct = (val / START - 1) * 100
    when = lambda t: dt.datetime.fromtimestamp(t, TZ).strftime("%a %-I:%M %p")
    eq = [START] + [x[1] for x in st["equity"]]
    lo, hi = min(eq + [START * 0.95]), max(eq + [START * 1.05])
    W, H = 600, 110
    path = " ".join(f"{'M' if i == 0 else 'L'}{i * W / max(1, len(eq) - 1):.1f},{H - (v - lo) / (hi - lo) * H:.1f}" for i, v in enumerate(eq))
    base_y = H - (START - lo) / (hi - lo) * H

    def votes(vs):
        rows = []
        for m in MEMBERS:
            ok, why = vs.get(m, [None, ""])
            tag = '<b class="y">YES</b>' if ok else '<b class="n">NO</b>'
            rows.append(f'<div class="vote">{tag}<span class="m">{m}</span><span class="w">{e(why)}</span></div>')
        return "".join(rows)

    def link(c):
        return f'https://www.geckoterminal.com/{c["net"]}/pools/{c["pool"]}'

    opn = "".join(
        f'<div class="card go"><div class="row"><b>{e(p["symbol"])} <span class="muted">{NETWORKS[p["net"]]}</span></b>'
        f'<span class="{"pos" if p["last"] >= p["entry"] else "neg"}">{(p["last"] / p["entry"] - 1) * 100:+.0f}%</span></div>'
        f'<div class="sub">in {price_fmt(p["entry"])} · now {price_fmt(p["last"])} · {money(p["cost"])} pretend · since {when(p["t"])}'
        f'{" · took half" if p.get("half") else ""} · <a href="{link(p)}">chart</a></div>'
        f'<details><summary>Why all five said YES</summary>{votes(p["votes"])}</details></div>' for p in st["open"]) \
        or '<p class="muted">No pretend trades open. Waiting for a coin all five members agree on.</p>'
    cls = "".join(
        f'<tr><td>{when(p["exit_t"])}</td><td>{e(p["symbol"])}</td><td>{e(p["why"])}</td>'
        f'<td class="{"pos" if p["pnl"] >= 0 else "neg"}">{p["pct"]:+.0f}%</td><td class="{"pos" if p["pnl"] >= 0 else "neg"}">{money(p["pnl"])}</td></tr>'
        for p in reversed(closed[-50:])) or '<tr><td class="muted">No closed pretend trades yet.</td></tr>'

    def card(c):
        w = c["whales"]
        nos = [m for m in MEMBERS if not c["votes"][m][0]]
        head = (f'<div class="row"><b>{e(c["symbol"])} <span class="muted">{NETWORKS[c["net"]]} · {price_fmt(c["price"])}</span></b>'
                f'<span class="{"pos" if c["ok"] else "muted"}">{"🧪 PRETEND BUY" if c["ok"] else "⏭ PASS"}</span></div>')
        stats = (f'<div class="mini"><span>Mkt cap <b>{money(c["mcap"])}</b></span><span>Liquidity <b>{money(c["liq"])}</b></span>'
                 f'<span>Vol 24h <b>{money(c["vol24"])}</b></span><span>Vol 1h <b>{money(c["vol1"])}</b></span>'
                 f'<span>1h <b class="{"pos" if c["h1"] >= 0 else "neg"}">{c["h1"]:+.0f}%</b></span><span>24h <b class="{"pos" if c["h24"] >= 0 else "neg"}">{c["h24"]:+.0f}%</b></span>'
                 f'<span>Top 10 wallets <b>{"—" if c["top10"] is None else str(c["top10"]) + "%"}</b></span>'
                 f'<span>🐋 buys <b class="pos">{money(w["buy_usd"])}</b> ({w["buys"]})</span><span>🐋 sells <b class="neg">{money(w["sell_usd"])}</b> ({w["sells"]})</span></div>')
        sub = f'<div class="sub">{when(c["t"])} · ' + (e(c["note"]) if c.get("note") else ("NO from " + ", ".join(nos) if nos else "all five YES")) + f' · <a href="{link(c)}">chart</a></div>'
        return f'<div class="card {"go" if c["ok"] else ""}">{head}{stats}{sub}<details><summary>All 5 votes</summary>{votes(c["votes"])}</details></div>'

    feed = "".join(card(c) for c in sorted(st["feed"], key=lambda c: -c["t"])[:40]) or '<p class="muted">First checks show up after the next run.</p>'
    last = when(st.get("last_run", time.time()))
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Meme Scout</title><style>
:root{{--bg:#0d0f1a;--card:#161a2b;--fg:#eef0fa;--muted:#9aa2c2;--line:#262c45;--ok:#45e39b;--bad:#ff6378;--acc:#c58bff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:16px}}h1{{font-size:22px;margin:4px 0}}h2{{font-size:16px;margin:22px 0 8px;color:var(--acc)}}
.muted{{color:var(--muted)}}.pos{{color:var(--ok)}}.neg{{color:var(--bad)}}a{{color:var(--acc)}}
.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}.k,.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px}}
.k small{{display:block;color:var(--muted);font-size:12px}}.k b{{font-size:19px}}.card{{margin-bottom:8px}}.card.go{{border-color:#6b3fa0}}
.row{{display:flex;justify-content:space-between;gap:8px}}.sub{{color:var(--muted);font-size:13px;margin:4px 0}}
.mini{{display:grid;grid-template-columns:repeat(3,1fr);gap:4px 10px;font-size:12px;color:var(--muted);margin-top:6px}}.mini b{{color:var(--fg)}}
.vote{{display:grid;grid-template-columns:40px 112px 1fr;gap:6px;font-size:13px;padding:3px 0;border-top:1px solid var(--line)}}
.vote .m{{font-weight:600}}.vote .w{{color:var(--muted)}}.y{{color:var(--ok)}}.n{{color:var(--bad)}}
table{{width:100%;border-collapse:collapse;font-size:13px}}td{{padding:6px 4px;border-top:1px solid var(--line)}}
details summary{{cursor:pointer;color:var(--muted);font-size:13px}}svg{{width:100%;height:auto;display:block}}.warn{{border-color:#7a5a1a;background:#2a2213}}
@media(max-width:420px){{.stats{{grid-template-columns:repeat(2,1fr)}}.mini{{grid-template-columns:repeat(2,1fr)}}.vote{{grid-template-columns:36px 100px 1fr}}}}
</style></head><body><main>
<p class="muted"><a href="index.html">← Bitcoin Scout</a> · paper test, pretend money, no wallet, no real orders</p>
<h1>🧪 Meme Scout</h1>
<p class="muted">Trending memecoins on Solana, Base and BNB Chain, checked for safety, whales and volume. Updated {last}.</p>
<div class="stats">
<div class="k"><small>Pretend account</small><b>${val:,.2f}</b></div>
<div class="k"><small>Profit</small><b class="{"pos" if val >= START else "neg"}">{"-" if val < START else ""}${abs(val - START):,.2f}</b></div>
<div class="k"><small>Return</small><b class="{"pos" if pct >= 0 else "neg"}">{pct:+.1f}%</b></div>
<div class="k"><small>Closed trades</small><b>{len(closed)}</b> <span class="muted">{wins}W–{len(closed) - wins}L</span></div>
<div class="k"><small>Open</small><b>{len(st["open"])}</b></div>
<div class="k"><small>Coins checked</small><b>{st.get("checks_total", 0)}</b></div>
</div>
<h2>Pretend account over time</h2><div class="k"><svg viewBox="0 0 {W} {H}" preserveAspectRatio="none">
<line x1="0" x2="{W}" y1="{base_y:.1f}" y2="{base_y:.1f}" stroke="#394065" stroke-dasharray="4 4"/><path d="{path}" fill="none" stroke="{"#45e39b" if val >= START else "#ff6378"}" stroke-width="2.5"/></svg></div>
<h2>Open pretend trades</h2>{opn}
<h2>Closed pretend trades</h2><div class="k"><table>{cls}</table></div>
<h2>The members' checks (latest coins)</h2>
<p class="muted">🐋 = whale trades of $5K+ in the last 24h. "Top 10 wallets" leaves out pools, locked and burned coins.</p>
{feed}
<h2>The rules</h2><div class="k"><ul>
<li><b>The Inspector:</b> you can sell it, the dev can't print more or freeze you, at least half the liquidity is locked, $75K+ liquidity.</li>
<li><b>The Whale Watcher:</b> top 10 wallets hold 30% or less (no single wallet over 8%), no insider wallets on top, whales bought at least as much as they sold in 24h.</li>
<li><b>The Volume:</b> $250K+ traded in 24h, $10K+ in the last hour, and not more than 25× its liquidity (fake volume check).</li>
<li><b>The Crowd:</b> more buyers than sellers in the last hour (at least 20 buyers).</li>
<li><b>The No-Chase:</b> up 2–30% this hour, under +150% today, at least 12 hours old.</li>
<li>Pretend buy = 10% of the account · sell all at −30% · sell half at +50% (then the rest can't lose) · sell the rest at +100% · 72-hour limit · ~1.5% fees+slippage each way.</li>
</ul></div>
<div class="k warn"><b>Heads up:</b> most memecoins go to zero, safety checks can't catch everything (rugs, coordinated dumps), and it only checks every 15 minutes, so real fills would be worse. This is a test, not advice.</div>
</main></body></html>"""


def main():
    st = load()
    log = []
    st["runs"] = st.get("runs", 0) + 1
    try:
        run(st, log)
    except Exception as e:
        log.append(f"run failed: {e}")
    st["last_run"] = time.time()
    json.dump(st, open(STATE, "w"), indent=0)
    os.makedirs("site", exist_ok=True)
    open("site/meme.html", "w", encoding="utf-8").write(page(st))
    print("\n".join(log))


if __name__ == "__main__":
    main()
