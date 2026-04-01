#!/usr/bin/env python3
"""
Backtesting Framework — compares parameter sets against live market data.

Discovers current markets, simulates trades with different configurations,
projects P&L outcomes, and recommends the best parameter set.

Usage:
  python3 backtest.py                    # Run default comparison
  python3 backtest.py --json             # Output JSON only
  python3 backtest.py --compare "yes_max=0.35,tp_roi=1.0,sl_pct=0.4"
"""
import json, subprocess, sys, time
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
BTDIR = STATE / "research" / "backtests"
BTDIR.mkdir(parents=True, exist_ok=True)

def bp(args, timeout=30):
    """Run bullpen command."""
    r = subprocess.run(["bullpen", "polymarket"] + args,
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return None, r.stderr[:300]
    try:
        return json.loads(r.stdout), None
    except:
        return r.stdout.strip(), None

def load_state():
    sfile = STATE / "state.json"
    if not sfile.exists():
        return {}
    with open(sfile) as f:
        return json.load(f)

def load_research():
    rfile = STATE / "research.json"
    if not rfile.exists():
        return {}
    with open(rfile) as f:
        return json.load(f)

def get_markets(scope="crypto", limit=100):
    """Fetch live markets for backtesting."""
    all_mkts = []
    d, e = bp(["discover", scope, "--min-liquidity", "100000",
               "--sort", "volume", "--limit", str(limit), "--output", "json"])
    if e or not d:
        return []
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except:
            return []
    
    for ev in d.get("events", []):
        for mk in ev.get("markets", []):
            if mk.get("closed") or mk.get("resolved"):
                continue
            outs = mk.get("outcomes", [])
            if len(outs) < 2:
                continue
            yp = outs[0].get("price") or 0
            np_ = outs[1].get("price") or 0
            if not yp or not np_:
                continue
            
            cat = _categorize(ev.get("title", "") + mk.get("question", ""))
            ed = ev.get("end_date", "")
            
            all_mkts.append({
                "slug": mk["slug"],
                "question": mk.get("question", ""),
                "yes_price": yp,
                "no_price": np_,
                "liquidity": mk.get("liquidity", 0),
                "volume_24h": mk.get("volume_24h", 0),
                "category": cat,
                "end_date": ed[:10] if ed else "TBD",
            })
    return all_mkts

def _categorize(text):
    """Classify a market question into a research category."""
    t = text.lower()
    if any(w in t for w in ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "doge", "bnb", "xrp", "crypto", "altcoin"]):
        return "crypto"
    if any(w in t for w in ["election", "vote", "politic", "president", "congress", "governor"]):
        return "politics"
    if any(w in t for w in ["war", "military", "weapon", "nuclear", "missile", "invasion"]):
        return "geopolitics"
    if any(w in t for w in ["recession", "gdp", "inflation", "cpi", "fed", "interest", "rate"]):
        return "macro"
    if any(w in t for w in ["sport", "nba", "nfl", "mlb", "nhl", "super bowl", "world cup", "fifa"]):
        return "sports"
    if any(w in t for w in ["oscar", "grammy", "emmy", "celebrity", "movie", "film", "album"]):
        return "entertainment"
    if any(w in t for w in ["tech", "ai", "apple", "google", "meta", "tesla", "nvidia", "startup"]):
        return "tech"
    return "general"

def simulate_trades(markets, config, name="unnamed"):
    """Simulate trades with given config against market list.
    
    config dict:
        yes_min, yes_max: YES buy range
        no_min, no_max: NO buy range (YES >= 1-no_max)
        max_pos: max positions
        trade_size: $ per trade
        tp_roi: take profit ROI
        sl_pct: stop loss percentage
    """
    yes_min = config.get("yes_min", 0.05)
    yes_max = config.get("yes_max", 0.40)
    no_min = config.get("no_min", 0.05)
    no_max = config.get("no_max", 0.15)
    max_pos = config.get("max_pos", 5)
    trade_size = config.get("trade_size", 1.0)
    tp_roi = config.get("tp_roi", 0.80)
    sl_pct = config.get("sl_pct", 0.50)
    
    positions = []
    trades = 0
    opportunities = []
    
    for m in markets:
        yp = m["yes_price"]
        np_ = m["no_price"]
        cat = m["category"]
        liq = m["liquidity"]
        vol = m["volume_24h"]
        ed = m["end_date"]
        
        outcome = None
        entry = None
        
        # Check YES buy
        if yes_min <= yp <= yes_max:
            outcome = "Yes"
            entry = yp
        # Check NO buy (when YES is very likely)
        elif yp >= (1 - no_max) and no_min <= np_ <= no_max:
            outcome = "No"
            entry = np_
        
        if not outcome:
            continue
        
        # Calculate expected ROI
        max_roi = round((1 - entry) / entry, 2)
        price_bucket = "t1" if entry <= 0.10 else "t2" if entry <= 0.20 else "t3" if entry <= 0.30 else "t4"
        
        opportunities.append({
            "slug": m["slug"],
            "question": m["question"],
            "outcome": outcome,
            "entry": entry,
            "max_roi": max_roi,
            "category": cat,
            "liquidity": liq,
            "volume_24h": vol,
            "end_date": ed,
            "price_bucket": price_bucket,
        })
    
    # Simulate filling positions (top ROI first)
    opportunities.sort(key=lambda x: -x["max_roi"])
    filled = opportunities[:max_pos]
    
    # Research-enhanced: weight outcomes by historical performance
    research = load_research()
    wr_adjust = 0.5  # default neutral win rate assumption for new markets
    cat_weights = {}
    bucket_weights = {}

    if research.get("by_category"):
        # Get category weights
        for cat, sc in research["by_category"].items():
            wr = sc.get("win_rate", 0.5)
            trades = sc.get("trades", 0)
            if trades >= 2:
                cat_weights[cat] = wr

    if research.get("by_price_bucket"):
        for b, sc in research["by_price_bucket"].items():
            wr = sc.get("win_rate", 0.5)
            trades = sc.get("trades", 0)
            if trades >= 2:
                bucket_weights[b] = wr
    
    # Calculate projected performance
    projected_wins = 0
    projected_loss_pnl = 0
    projected_win_pnl = 0
    total_wagered = 0
    
    for trade in filled:
        base_wr = 0.5
        
        # Adjust by category performance
        cat = trade["category"]
        if cat in cat_weights:
            base_wr = cat_weights[cat]
        
        # Average with outcome performance
        if research.get("by_outcome"):
            outcome_wr = research["by_outcome"].get(trade["outcome"], {}).get("win_rate", 0.5)
            base_wr = (base_wr + outcome_wr) / 2
        
        # Adjust by price bucket performance
        pb = trade["price_bucket"]
        if pb in bucket_weights:
            base_wr = (base_wr + bucket_weights[pb]) / 2
        
        # Simulate outcome
        if base_wr >= 0.45:  # positive expectation
            # TP outcome
            projected_wins += 1
            tp_pnl = trade_size * tp_roi
            projected_win_pnl += tp_pnl
            trade["sim_outcome"] = "TP"
            trade["sim_wr"] = round(base_wr, 3)
            trade["sim_pnl"] = round(tp_pnl, 2)
        else:
            # SL outcome
            sl_pnl = -trade_size * sl_pct
            projected_loss_pnl += sl_pnl
            trade["sim_outcome"] = "SL"
            trade["sim_wr"] = round(base_wr, 2)
            trade["sim_pnl"] = round(sl_pnl, 2)
        
        total_wagered += trade_size
    
    projected_pnl = projected_win_pnl + projected_loss_pnl
    proj_wr = projected_wins / len(filled) if filled else 0
    
    return {
        "name": name,
        "config": config,
        "total_markets_scanned": len(markets),
        "opportunities_found": len(opportunities),
        "positions_filled": len(filled),
        "total_wagered": total_wagered,
        "projected_wr": round(proj_wr, 3),
        "projected_pnl": round(projected_pnl, 2),
        "projected_roi": round(projected_pnl / total_wagered, 3) if total_wagered > 0 else 0,
        "projected_wins": projected_wins,
        "projected_losses": len(filled) - projected_wins,
        "avg_max_roi": round(sum(t["max_roi"] for t in filled) / len(filled), 2) if filled else 0,
        "filled_trades": filled[:10],  # top 10
        "all_opportunities": opportunities[:20],
    }

def default_configs():
    """Generate parameter sets to compare."""
    state = load_state()
    research = load_research()
    
    # Current live config
    current = {
        "yes_min": state.get("yes_min", 0.05),
        "yes_max": state.get("yes_max", 0.40),
        "no_min": 0.05,
        "no_max": 0.15,
        "max_pos": state.get("max_pos", 5),
        "trade_size": state.get("trade", 1.0),
        "tp_roi": state.get("tp_roi", 0.80),
        "sl_pct": state.get("sl_pct", 0.50),
    }
    
    # Generate variants
    configs = [
        {"name": "CURRENT (live)", "config": current.copy()},
        {"name": "Aggressive (+more positions)", "config": {**current, "max_pos": 8}},
        {"name": "Conservative (tight YES)", "config": {**current, "yes_max": 0.25}},
        {"name": "Conservative (higher TP)", "config": {**current, "tp_roi": 1.0}},
        {"name": "Aggressive (wide YES)", "config": {**current, "yes_max": 0.55}},
        {"name": "Balanced (wide YES, higher TP)", "config": {**current, "yes_max": 0.50, "tp_roi": 0.90}},
        {"name": "Tight Risk (low SL)", "config": {**current, "sl_pct": 0.30}},
    ]
    
    # Research-driven: customize based on research data
    if research.get("by_category"):
        best_cats = [(cat, sc["win_rate"], sc["trades"])
                     for cat, sc in research["by_category"].items()
                     if sc.get("trades", 0) >= 2]
        best_cats.sort(key=lambda x: -x[1])
        
        if best_cats:
            top_cat, top_wr, top_n = best_cats[0]
            configs.append({
                "name": f"Research-tuned (focus: {top_cat})",
                "config": {**current, "yes_max": min(0.50, current.get("yes_max", 0.40) + 0.10)}
            })
    
    return configs

def run_backtest(configs=None, json_output=False):
    """Run full backtest comparison."""
    print("📊 Fetching live markets..." if not json_output else "")
    markets = get_markets(scope="crypto", limit=100)
    
    if not markets:
        print("❌ No markets found. Bullpen CLI may be unavailable.")
        return []
    
    print(f"📊 Scanned {len(markets)} markets" if not json_output else "")
    
    if configs is None:
        configs = default_configs()
    
    results = []
    for i, cfg_info in enumerate(configs):
        name = cfg_info["name"]
        config = cfg_info["config"]
        
        res = simulate_trades(markets, config, name)
        results.append(res)
        
        if not json_output:
            print(f"\n{'='*60}")
            print(f"📋 {name}")
            print(f"{'='*60}")
            print(f"  Markets scanned:    {res['total_markets_scanned']}")
            print(f"  Opportunities:      {res['opportunities_found']}")
            print(f"  Positions filled:   {res['positions_filled']}")
            print(f"  Total wagered:      ${res['total_wagered']:.2f}")
            print(f"  Projected win rate: {res['projected_wr']:.1%}")
            print(f"  Projected P&L:      ${res['projected_pnl']:+.2f}")
            print(f"  Projected ROI:      {res['projected_roi']:.1%}")
            print(f"  Avg max ROI:        {res['avg_max_roi']:.1f}x")
            
            if res['filled_trades']:
                print(f"\n  Top {len(res['filled_trades'])} trades:")
                for j, t in enumerate(res['filled_trades'][:5], 1):
                    print(f"    {j}. {t['question'][:50]}...")
                    print(f"       {t['outcome']} @ {t['entry']:.0%} → max {t['max_roi']:.1f}x "
                          f"[{t['category']}] [{t['price_bucket']}]")
                    print(f"       Sim: {t['sim_outcome']} (est WR: {t['sim_wr']:.0%}, P&L: ${t['sim_pnl']:+.2f})")
    
    # Save backtest results
    bt_result = {
        "run_at": datetime.now().isoformat(),
        "markets_scanned": len(markets),
        "results": results,
        "best_config": max(results, key=lambda x: x["projected_pnl"])["name"] if results else "N/A",
        "recommendation": [],
    }
    
    # Find winner
    if len(results) >= 2:
        current_pnl = results[0]["projected_pnl"]
        others = results[1:]
        best = max(others, key=lambda x: x["projected_pnl"])
        
        if best["projected_pnl"] > current_pnl * 1.2:  # 20% improvement
            bt_result["recommendation"].append(
                f"🎯 Consider switching to '{best['name']}': "
                f"${best['projected_pnl']:+.2f} projected vs ${current_pnl:+.2f} current "
                f"({abs(best['projected_pnl'] - current_pnl):.2f} improvement)"
            )
            bt_result["recommendation"].append(
                f"   Config: {json.dumps(best['config'])}"
            )
        else:
            bt_result["recommendation"].append(
                "✅ Current config is competitive — no change needed"
            )
    
    # Save to file
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    btfile = BTDIR / f"backtest_{ts}.json"
    bt_result["best_config"] = max(results, key=lambda x: x["projected_pnl"])["name"] if results else "N/A"
    btfile.write_text(json.dumps(bt_result, indent=2))
    
    if not json_output:
        print(f"\n{'='*60}")
        print(f"📊 BACKTEST SUMMARY")
        print(f"{'='*60}")
        print(f"  Markets: {len(markets)}")
        print(f"  Best config: {bt_result['best_config']}")
        for r, rec in enumerate(bt_result["recommendation"], 1):
            print(f"  {r}. {rec}")
    
    return results

if __name__ == "__main__":
    json_out = "--json" in sys.argv
    configs = None
    
    # Parse --compare "key=value,key=value"
    for arg in sys.argv[1:]:
        if arg.startswith("--compare=") or (arg.startswith("--compare") and "=" in arg):
            params_str = arg.split("=", 1)[1] if "=" in arg else ""
            if params_str:
                custom_config = {}
                for pair in params_str.split(","):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        try:
                            custom_config[k.strip()] = float(v.strip())
                        except:
                            custom_config[k.strip()] = v.strip()
                
                state = load_state()
                base = {
                    "yes_min": state.get("yes_min", 0.05),
                    "yes_max": state.get("yes_max", 0.40),
                    "no_min": 0.05,
                    "no_max": 0.15,
                    "max_pos": state.get("max_pos", 5),
                    "trade_size": state.get("trade", 1.0),
                    "tp_roi": state.get("tp_roi", 0.80),
                    "sl_pct": state.get("sl_pct", 0.50),
                }
                configs = [{"name": f"Custom ({params_str})", "config": {**base, **custom_config}}]
    
    run_backtest(configs=configs, json_output=json_out)
