#!/usr/bin/env python3
"""
Polymarket Autonomous Agent — complete trading lifecycle manager.

Research → Score → Buy → Monitor → Exit → Journal → Improve
Auto-compounding, multi-strategy, position exit management.
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ─── Paths ───────────────────────────────────────────────────────
REPO  = Path(__file__).resolve().parent
STATE = REPO / "state"
LOGS  = REPO / "data" / "logs"
JOURN = REPO / "data" / "journals"
for d in [STATE, LOGS, JOURN]:
    d.mkdir(parents=True, exist_ok=True)

# ─── Default Config ─────────────────────────────────────────────
DEFAULT = dict(
    max_positions       = 5,
    trade_size          = 1.00,
    max_trade           = 2.50,
    daily_loss_limit    = 3.00,
    total_cap           = 5.00,
    yes_buy_min         = 0.05,
    yes_buy_max         = 0.40,
    no_buy_min          = 0.05,
    no_buy_max          = 0.15,
    take_profit_roi     = 0.80,
    stop_loss_pct       = 0.50,
    min_liquidity       = 200000,
    min_volume          = 100000,
    scopes              = ["crypto"],
    auto_compound       = True,
    profit_step         = 0.25,
    loss_step           = 0.25,
    compounding_every   = 3.00,
)


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")
    with open(LOGS / f"agent_{datetime.now():%Y-%m-%d}.log", "a") as f:
        f.write(f"[{ts}] [{level}] {msg}\n")


# ─── State ──────────────────────────────────────────────────────
SFILE = STATE / "state.json"

def load_state():
    if SFILE.exists():
        try:
            s = json.loads(SFILE.read_text())
            return s
        except:
            pass
    return dict(
        config     = DEFAULT.copy(),
        positions  = {},    # slug → {outcome, price, amount, added}
        journal    = [],    # [{date, type, result, pnl}]
        daily_pnl  = 0.0,
        total_pnl  = 0.0,
        runs       = 0,
    )

def save_state(s):
    s["last_run"] = datetime.now().isoformat()
    SFILE.write_text(json.dumps(s, indent=2))


# ─── Bullpen CLI ────────────────────────────────────────────────
def bp(args, timeout=30):
    cmd = ["bullpen", "polymarket"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return None, r.stderr.strip()[:300]
        try:
            return json.loads(r.stdout), None
        except:
            return r.stdout.strip(), None
    except Exception as e:
        return None, str(e)


# ─── Safety ─────────────────────────────────────────────────────
def check_balance(s):
    cap = s["config"]["total_cap"]
    d, e = bp(["clob", "balance"])
    if e:
        log(f"⚠️ balance: {e}", "WARN")
        return False
    t = d if isinstance(d, str) else ""
    bal = 0.0
    for ln in t.split("\n"):
        if "Balance:" in ln:
            try:
                bal = float(ln.split("$")[1].split(",")[0])
            except:
                pass
    if bal <= 0:
        log(f"🛑 Balance ${bal:.2f}", "CRIT");
        return False
    if bal < cap:
        log(f"** Balance ${bal:.2f} below ${cap:.2f}", "WARN")
    log(f"💰 ${bal:.2f} | Daily ${s['daily_pnl']:+.2f} | Tot ${s['total_pnl']:+.2f}")
    if s["daily_pnl"] <= -s["config"]["daily_loss_limit"]:
        log(f"🛑 daily loss hit", "CRIT")
        return False
    return True


# ─── Real Polymarket Positions ──────────────────────────────────
def fetch_positions():
    d, e = bp(["positions"])
    if e or not d:
        return []
    t = d if isinstance(d, str) else ""
    if "No active" in t or "Portfolio Value: $0.00" in t:
        return []
    lst = []
    for ln in t.split("\n"):
        ln = ln.strip()
        if not ln or ln.startswith(("Showing", "Portfolio", "Market", "—")):
            continue
        if "$" in ln and "%" in ln:
            parts = ln.split()
            pnl = 0.0
            for p in parts:
                if "%" in p:
                    try:
                        pnl = float(p.replace("%", ""))
                    except:
                        pass
            lst.append(dict(
                raw   = ln,
                mkt   = parts[0] if parts else "",
                pnl   = pnl,
            ))
    return lst


# ─── Discover ───────────────────────────────────────────────────
def discover(scopes):
    """Return flat list of market dicts."""
    all_m = []
    for sc in scopes:
        d, e = bp(["discover", sc, "--min-liquidity", "200000",
                   "--min-volume", "100000", "--sort", "volume",
                   "--limit", "50", "--output", "json"])
        if e:
            log(f"⚠ {sc}: {e}", "WARN")
            continue
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except:
                continue
        for ev in d.get("events", []):
            for mkt in ev.get("markets", []):
                if mkt.get("closed") or mkt.get("resolved"):
                    continue
                all_m.append(dict(
                    slug   = mkt.get("slug", ""),
                    q      = mkt.get("question", ""),
                    end    = ev.get("end_date", ""),
                    vol    = mkt.get("volume_24h", 0),
                    liq    = mkt.get("liquidity", 0),
                    outs   = mkt.get("outcomes", []),
                ))
    return all_m


# ─── Score ──────────────────────────────────────────────────────
def score(m, cfg):
    """Return list of {slug,q,outcome,price,roi, strat}."""
    o = m["outs"]
    if len(o) < 2:
        return []
    yp = o[0].get("price") or 0
    np_ = o[1].get("price") or 0
    if not yp or not np_:
        return []
    r = []
    # YES value
    if cfg["yes_buy_min"] <= yp <= cfg["yes_buy_max"]:
        r.append(dict(
            slug   = m["slug"], q   = m["q"],
            end    = m["end"][:10] if m["end"] else "TBD",
            strat  = "yes_val",
            outcome = "Yes", price = yp,
            roi    = round((1 - yp) / yp, 2),
        ))
    # NO hedge  (YES ≥ 85% → NO ≤ 15%)
    if yp >= 0.85 and cfg["no_buy_min"] <= np_ <= cfg["no_buy_max"]:
        r.append(dict(
            slug   = m["slug"], q   = m["q"],
            end    = m["end"][:10] if m["end"] else "TBD",
            strat  = "no_hedge",
            outcome = "No", price = np_,
            roi    = round((1 - np_) / np_, 2),
        ))
    return r


# ─── Trade ──────────────────────────────────────────────────────
def buy(slug, outcome, amt, dry):
    if dry:
        return True, "dry"
    d, e = bp(["buy", slug, outcome, f"{amt:.2f}", "--yes", "--output", "json"])
    if e:
        return False, e
    oid = ""
    if isinstance(d, dict):
        oid = d.get("order_id") or d.get("id") or "ok"
    return True, oid


def sell(slug, outcome, amt, dry):
    if dry:
        return True, "dry"
    d, e = bp(["sell", slug, outcome, f"{amt:.2f}", "--yes", "--output", "json"])
    if e:
        return False, e
    return True, "sold"


# ─── Size ───────────────────────────────────────────────────────
def calc_size(s):
    c = s["config"]
    if not c["auto_compound"]:
        return round(min(c["trade_size"], c["max_trade"]), 2)
    p = s["total_pnl"]
    base = c["trade_size"]
    step = c["profit_step"] if p >= 0 else -c["loss_step"]
    n = int(abs(p) // c["compounding_every"]) if c["compounding_every"] else 0
    sz = base + n * step if p >= 0 else base - n * c["loss_step"]
    return round(max(0.50, min(sz, c["max_trade"])), 2)


# ─── Exits ──────────────────────────────────────────────────────
def manage_exits(s, dry):
    c  = s["config"]
    ps = s.get("positions", {})
    if not ps:
        return
    real = fetch_positions()
    closed = 0
    for slug in list(ps.keys()):
        info = ps[slug]
        entry_cost = info["price"] * info["amount"]
        # Find matching real position by slug substring
        match = None
        for rp in real:
            if slug in rp.get("raw", "") or slug in rp.get("mkt", ""):
                match = rp
                break
        if not match:
            log(f"⏳ pending? → {slug[:50]}", "INFO")
            continue
        pnl_pct = match["pnl"] / 100
        current_value = entry_cost * (1 + pnl_pct)
        pnl_d   = current_value - entry_cost
        roi     = pnl_d / entry_cost if entry_cost > 0 else 0

        # Take profit
        if roi >= c["take_profit_roi"]:
            log(f"💰 TP → {slug[:50]} ROI {roi:.0%} (${pnl_d:+.2f})")
            ok, m = sell(slug, info["outcome"], info["amount"], dry)
            if ok:
                s["total_pnl"] += pnl_d
                s["daily_pnl"] += pnl_d
                jlog(s, "sell_profit", slug, pnl_d)
                del ps[slug]
                closed += 1
            continue

        # Stop loss
        if roi <= -c["stop_loss_pct"]:
            log(f"🛑 SL → {slug[:50]} ROI {roi:.0%} (${pnl_d:+.2f})")
            ok, m = sell(slug, info["outcome"], info["amount"], dry)
            if ok:
                s["total_pnl"] += pnl_d
                s["daily_pnl"] += pnl_d
                jlog(s, "sell_loss", slug, pnl_d)
                del ps[slug]
                closed += 1
            continue

    if closed:
        log(f"🧹 closed {closed}")


# ─── Journal ─────────────────────────────────────────────────────
def jlog(s, kind, slug, pnl=0.0):
    JOURN.mkdir(parents=True, exist_ok=True)
    jfile = JOURN / "strategy_journal.json"
    rec = dict(
        date  = datetime.now().isoformat(),
        slug  = slug,
        type  = kind,
        pnl   = round(pnl, 2),
    )
    lst = []
    if jfile.exists():
        try:
            lst = json.loads(jfile.read_text())
        except:
            pass
    lst.append(rec)
    jfile.write_text(json.dumps(lst, indent=2))


# ─── The Agent ─────────────────────────────────────────────────────
def run(dry=True):
    mode = "DRY" if dry else "LIVE"
    s    = load_state()
    c    = s["config"]
    sz   = calc_size(s)

    log("=" * 70)
    log(f"POLY AUTONOMOUS AGENT [{mode}]")
    log(f"Run #{s['runs']+1}  {datetime.now():%Y-%m-%d %H:%M}  size~${sz:.2f}")

    # 1. Safety
    if not check_balance(s):
        save_state(s); return

    # 2. Exit positions
    manage_exits(s, dry)

    # 3. Discover
    mkts = discover(c["scopes"])
    log(f"🔍 {len(mkts)} markets")

    # 4. Score
    all_o = []
    for m in mkts:
        all_o.extend(score(m, c))
    all_o.sort(key=lambda x: -x["roi"])

    exist = set(s.get("positions", {}).keys())
    avail = [o for o in all_o if o["slug"] not in exist]

    log(f"🎯 {len(avail)} new ({sz} avail)")
    for i, o in enumerate(avail[:8], 1):
        log(f"  {i}. B {o['outcome']}@{o['price']:.0%} {o['roi']}x {o['q'][:60]}...")

    # 5. Execute
    n = len(s.get("positions", {}))
    slots = c["max_positions"] - n
    filled = 0
    for o in avail[:slots]:
        slug = o["slug"]
        ok, m = buy(slug, o["outcome"], sz, dry)
        if ok:
            s["positions"][slug] = dict(
                outcome = o["outcome"],
                price   = o["price"],
                amount  = sz,
                added   = datetime.now().isoformat(),
            )
            filled += 1
            log(f"  ✅ {slug[:50]}")
            jlog(s, "open", slug)
        else:
            log(f"  ❌ {slug[:40]}… {m}", "ERR")

    s["runs"] += 1
    log(f"📊 {n+filled}/{c['max_positions']} | PnL ${s['total_pnl']:+.2f}")
    log("=" * 70)
    save_state(s)


def main():
    args = sys.argv[1:]
    dry  = "--live" not in args   # safety default
    run(dry)


if __name__ == "__main__":
    main()