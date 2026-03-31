#!/usr/bin/env python3
"""
Polymarket Auto-Trader — discovers opportunities via Bullpen CLI,
identifies mispriced markets, and places micro-trades ($1.00).

No external APIs. No Twitter. No Kaggle. No sentiment scraping.
Pure Bullpen CLI + price inefficiency detection.

Usage:
  python3 polymarket_trader.py              # Dry run (no orders)
  python3 polymarket_trader.py --live       # Execute real orders
  python3 polymarket_trader.py --status     # Show balance + positions
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ─── Paths ───────────────────────────────────────────────────────────
REPO    = Path(__file__).resolve().parent
LOGS    = REPO / "data" / "logs"
JOURNAL = REPO / "data" / "trade-journal"
LOGS.mkdir(parents=True, exist_ok=True)
JOURNAL.mkdir(parents=True, exist_ok=True)

# ─── User Config ─────────────────────────────────────────────────────
TRADE_SIZE_USD      = 1.00       # Micro-trade
MAX_DAILY_LOSS_USD  = 3.00       # Stop after this loss
MAX_TRADES_PER_RUN  = 3          # Max orders per run
MIN_LIQUIDITY       = 200000     # $200k min liquidity
MIN_VOLUME_24H      = 100000     # $100k min daily volume
# ─── Opportunity thresholds ───
YES_BUY_MIN         = 0.05       # Buy YES ≥ 5¢
YES_BUY_MAX         = 0.40       # Buy YES ≤ 40¢ (cheap = value play)
NO_BUY_MIN          = 0.05       # Buy NO ≥ 5¢
NO_BUY_MAX          = 0.15       # Buy NO ≤ 15¢ (very unlikely but big payoff)
# ─── End Config ──────────────────────────────────────────────────────


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")
    with open(LOGS / f"trader_{datetime.now():%Y-%m-%d}.log", "a") as f:
        f.write(f"[{ts}] {msg}\n")


def bullpen(args, timeout=30):
    """Run `bullpen polymarket <args>` and return (parsed_data, error)."""
    cmd = ["bullpen", "polymarket"] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return None, r.stderr.strip()[:300]
    try:
        return json.loads(r.stdout), None
    except json.JSONDecodeError:
        return r.stdout.strip(), None


# ─── Safety ──────────────────────────────────────────────────────────

def get_balance():
    data, err = bullpen(["clob", "balance"])
    if err:
        return None, err
    txt = data if isinstance(data, str) else ""
    for line in txt.split("\n"):
        if "Balance:" in line:
            try:
                return float(line.split("$")[1].split(",")[0]), None
            except (ValueError, IndexError):
                pass
    return None, "could not parse balance"


def get_positions():
    data, err = bullpen(["positions"])
    if err:
        return 0, []
    txt = data if isinstance(data, str) else ""
    if "No active positions" in txt or "Portfolio Value: $0.00" in txt:
        return 0, []
    lines = []
    for line in txt.split("\n"):
        line = line.strip()
        if "$" in line and "%" in line and not line.startswith("Market") and not line.startswith("---"):
            lines.append(line)
    return len(lines), lines


def daily_pnl():
    today = f"{datetime.now():%Y-%m-%d}"
    jfile = JOURNAL / f"trades_{today}.csv"
    if not jfile.exists():
        return 0.0
    total = 0.0
    with open(jfile) as f:
        header = None
        for line in f:
            line = line.strip()
            if not line:
                continue
            if header is None:
                header = line.split(",")
                continue
            vals = line.split(",")
            try:
                pnl_i = header.index("pnl")
                total += float(vals[pnl_i])
            except (ValueError, IndexError):
                pass
    return total


# ─── Discovery ───────────────────────────────────────────────────────

def discover_opportunities(scope="crypto"):
    """Discover markets via Bullpen CLI discover command."""
    data, err = bullpen([
        "discover", scope,
        "--min-liquidity", str(MIN_LIQUIDITY),
        "--min-volume", str(MIN_VOLUME_24H),
        "--sort", "volume", "--limit", "50",
        "--output", "json",
    ], timeout=30)
    if err:
        log(f"⚠️ discover error: {err}")
        return []

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            log("⚠️ discover output not valid JSON")
            return []

    markets = []
    for event in data.get("events", []):
        for mkt in event.get("markets", []):
            if mkt.get("closed") or mkt.get("resolved"):
                continue
            markets.append({
                "event":       event.get("title", ""),
                "question":    mkt.get("question", ""),
                "slug":        mkt.get("slug", ""),
                "market_id":   mkt.get("id", ""),
                "volume_24h":  mkt.get("volume_24h", 0),
                "liquidity":   mkt.get("liquidity", 0),
                "outcomes":    mkt.get("outcomes", []),
                "ends":        event.get("end_date", ""),
            })
    return markets


def score_opportunity(m):
    """
    Score a single market. Returns dict with trade decision
    or None if not actionable.
    """
    outcomes = m["outcomes"]
    if len(outcomes) < 2:
        return None

    yes = outcomes[0]
    no = outcomes[1]
    yes_price = yes.get("price") or 0
    no_price = no.get("price") or 0
    if not yes_price or not no_price:
        return None

    opp = None

    # BUY YES when cheap (5¢-40¢) — high ROI if it happens
    if YES_BUY_MIN <= yes_price <= YES_BUY_MAX:
        opp = {"outcome": "Yes", "entry_price": yes_price,
               "max_roi": round((1 - yes_price) / yes_price, 1)}

    # BUY NO when very unlikely (≥ 85¢ YES → ≤ 15¢ NO)
    # Only if no_price is still ≥ 5¢ (not worthless)
    elif yes_price >= 0.85 and NO_BUY_MIN <= no_price <= NO_BUY_MAX:
        opp = {"outcome": "No", "entry_price": no_price,
               "max_roi": round((1 - no_price) / no_price, 1)}

    if not opp:
        return None

    return {
        "slug":         m["slug"],
        "event":        m["event"],
        "question":     m["question"],
        "yes_price":    yes_price,
        "no_price":     no_price,
        "volume_24h":   m["volume_24h"],
        "liquidity":    m["liquidity"],
        "ends":         m["ends"][:10] if m.get("ends") else "TBD",
        "outcome":      opp["outcome"],
        "entry_price":  opp["entry_price"],
        "max_roi":      opp["max_roi"],
        "score":        opp["max_roi"],  # rank by ROI
    }


# ─── Execution ───────────────────────────────────────────────────────

def place_order(slug, outcome, amount, dry_run=False):
    """Execute `bullpen polymarket buy <...>`. Returns (success, msg)."""
    if dry_run:
        return True, f"dry_run_{outcome}_on_{slug}"

    data, err = bullpen([
        "buy", slug, outcome, f"{amount:.2f}",
        "--yes", "--output", "json",
    ], timeout=30)
    if err:
        return False, err
    order_id = "placed"
    if isinstance(data, dict):
        order_id = data.get("order_id") or data.get("id") or "placed"
    return True, order_id


def journal(opportunity, success, msg, amount=TRADE_SIZE_USD, pnl=0.0):
    JOURNAL.mkdir(parents=True, exist_ok=True)
    today = f"{datetime.now():%Y-%m-%d}"
    jfile = JOURNAL / f"trades_{today}.csv"
    header = "timestamp,question,slug,outcome,amount,pnl,success,result"
    if not jfile.exists():
        with open(jfile, "w") as f:
            f.write(header + "\n")
    q = opportunity["question"].replace('"', '""')
    with open(jfile, "a") as f:
        f.write(f'"{datetime.now().isoformat()}","{q}",{opportunity["slug"]}'
                f',{opportunity["outcome"]},{amount:.2f},{pnl:.2f},'
                f'{success},"{msg}"\n')


# ─── Main ─────────────────────────────────────────────────────────────

def cmd_status():
    """Show quick status summary."""
    bal, err = get_balance()
    if bal is not None:
        log(f"Balance:  ${bal:.2f}")
    else:
        log(f"Balance:  ??? ({err})")

    npos, _pos = get_positions()
    log(f"Positions: {npos} open")

    dpnl = daily_pnl()
    log(f"Daily PnL: ${dpnl:+.2f}")
    return 0


def cmd_run(live=False):
    mode = "LIVE" if live else "DRY RUN"
    log("=" * 60)
    log(f"POLYMARKET AUTO-TRADER [{mode}]")
    log(f"  trade_size=${TRADE_SIZE_USD} | daily_loss_limit=${MAX_DAILY_LOSS_USD}")
    log(f"  YES buy: {YES_BUY_MIN:.0%}-{YES_BUY_MAX:.0%} | NO buy: 85%+")
    log("=" * 60)

    # Safety checks
    bal, err = get_balance()
    if bal is not None:
        log(f"Balance: ${bal:.2f}")
        if bal < TRADE_SIZE_USD:
            log("⚠️ Insufficient balance"); return
    else:
        log(f"⚠️ Balance check failed: {err}")

    npos, _ = get_positions()
    log(f"Open positions: {npos}")

    dpnl = daily_pnl()
    log(f"Daily PnL: ${dpnl:+.2f}")
    if dpnl <= -MAX_DAILY_LOSS_USD:
        log("🛑 Daily loss limit hit — stopping")
        return

    # Discover markets across crypto and general
    all_markets = []
    for scope in ["crypto"]:  # start with crypto
        mkt = discover_opportunities(scope)
        all_markets.extend(mkt)
    log(f"Discovered {len(all_markets)} qualifying markets")

    # Score every market
    opps = []
    for m in all_markets:
        s = score_opportunity(m)
        if s:
            opps.append(s)
    # Sort by ROI (highest potential first)
    opps.sort(key=lambda x: -x["score"])

    log(f"Found {len(opps)} opportunities:")
    for i, o in enumerate(opps[:10], 1):
        log(f"  {i}. {o['question'][:65]}...")
        log(f"     Buy {o['outcome']} @ {o['entry_price']:.0%}  →  max ROI: {o['max_roi']}x")

    # Execute trades
    trades = 0
    slots = min(MAX_TRADES_PER_RUN, 10 - npos)  # keep some headroom
    for opp in opps[:slots]:
        log(f"\n▶ Trading: {opp['question'][:60]}...")
        log(f"  Buy {opp['outcome']} @ {opp['entry_price']:.0%}  amount=${TRADE_SIZE_USD:.2f}")
        ok, msg = place_order(opp["slug"], opp["outcome"], TRADE_SIZE_USD, dry_run=not live)
        journal(opp, ok, msg, pnl=0.0)
        trades += 1
        if ok:
            log(f"  ✅ Order: {msg}")
        else:
            log(f"  ❌ Failed: {msg}")

    log(f"\nExecuted {trades} trade(s) ${trades * TRADE_SIZE_USD:.2f}")
    log(f"  {'🔴 LIVE ORDERS PLACED' if live else '🟡 DRY RUN — no real orders'}")
    log("=" * 60)


def main():
    args = sys.argv[1:]
    if "--status" in args:
        sys.exit(cmd_status())
    elif "--live" in args:
        sys.exit(cmd_run(live=True))
    else:
        sys.exit(cmd_run(live=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        import traceback; log(traceback.format_exc())
        sys.exit(1)
