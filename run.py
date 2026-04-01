#!/usr/bin/env python3
"""
Orchestrator — autonomous cycle manager for the Polymarket trading system.

Cycles: Research → Incubate → Trade → Review
Runs on every cron trigger (every 4h).

Usage:
  python3 run.py              # Full cycle (dry run)
  python3 run.py --live       # Full cycle (real trades)
  python3 run.py --trade-only # Just trading, skip research/backtest
  python3 run.py --status     # Show system status
"""
import json, subprocess, sys, time
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
LOGS = ROOT / "data/logs"
for d in [STATE, LOGS]: d.mkdir(parents=True, exist_ok=True)

SFILE = STATE / "state.json"
STRAT = STATE / "strategies.json"
OFILE = STATE / "orchestrator.json"

def log(msg, level="INFO"):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")
    with open(LOGS / f"orchestrator_{time.strftime('%Y-%m-%d')}.log", "a") as f:
        f.write(f"[{ts}] [{level}] {msg}\n")

def load_json(path):
    if not path.exists():
        return {}
    with open(path) as f:
        try:
            return json.load(f)
        except:
            return {}

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def get_orchestrator_state():
    d = load_json(OFILE)
    d.setdefault("total_runs", 0)
    d.setdefault("last_research", 0)
    d.setdefault("last_backtest", 0)
    d.setdefault("last_incubate", 0)
    d.setdefault("cycles_since_promotion", 0)
    d.setdefault("promotions", [])
    d.setdefault("rollbacks", [])
    d.setdefault("phase", "trade")  # research, incubate, trade
    d.setdefault("last_run_at", "")
    return d

def save_orchestrator_state(d):
    d["last_run_at"] = datetime.now().isoformat()
    save_json(OFILE, d)

def get_strategies():
    if not STRAT.exists():
        # Init with current config
        state = load_json(SFILE)
        active = {
            "name": "default",
            "config": {
                "yes_min": state.get("yes_min", 0.05),
                "yes_max": state.get("yes_max", 0.40),
                "max_pos": state.get("max_pos", 5),
                "trade_size": state.get("trade", 1.0),
                "tp_roi": state.get("tp_roi", 0.80),
                "sl_pct": state.get("sl_pct", 0.50),
            },
            "status": "active",
            "since": datetime.now().isoformat(),
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
            "win_rate": 0.0,
        }
        strategies = {"active": active, "incubating": [], "history": []}
        save_json(STRAT, strategies)
        return strategies
    return load_json(STRAT)

def save_strategies(s):
    save_json(STRAT, s)

def promote_strategy(strategies, candidate_name):
    """Promote incubating strategy to active."""
    active = strategies["active"]
    
    # Move old active to history
    active["status"] = "demoted"
    active["demoted_at"] = datetime.now().isoformat()
    strategies["history"].append(active)
    
    # Find and promote candidate
    for i, inc in enumerate(strategies["incubating"]):
        if inc["name"] == candidate_name:
            inc["status"] = "active"
            inc["promoted_at"] = datetime.now().isoformat()
            inc["trades"] = 0
            inc["wins"] = 0
            inc["losses"] = 0
            inc["pnl"] = 0.0
            strategies["active"] = inc
            strategies["incubating"].pop(i)
            log(f"🎯 Promoted '{candidate_name}' to active", "PROMOTE")
            return True
    
    return False

def start_incubation(strategies, config_name, config):
    """Start incubating a new strategy variant."""
    # Check if already incubating
    for inc in strategies["incubating"]:
        if inc["name"] == config_name:
            return False
    
    inc = {
        "name": config_name,
        "config": config,
        "status": "incubating",
        "started_at": datetime.now().isoformat(),
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "pnl": 0.0,
        "win_rate": 0.0,
    }
    
    strategies["incubating"].append(inc)
    log(f"🧪 Started incubating '{config_name}'", "INCUBATE")
    return True

def run_phase_research():
    """Run research analysis."""
    log("🔬 Phase: Research", "PHASE")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("research", ROOT / "research.py")
        research = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(research)
        r = research.analyze()
        research.save_research(r)
        
        log(f"📊 Win rate: {r['summary']['win_rate']:.1%}", "RESEARCH")
        log(f"📈 Closed trades: {r['summary']['closed_trades']}", "RESEARCH")
        if r.get("recommendations"):
            for rec in r["recommendations"]:
                log(f"  {rec}", "RESEARCH")
        return r
    except Exception as e:
        log(f"⚠️ Research failed: {e}", "ERROR")
        return None

def run_phase_backtest():
    """Run backtest and return recommendations."""
    log("📊 Phase: Backtest", "PHASE")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("backtest", ROOT / "backtest.py")
        backtest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backtest)
        
        # Run backtest
        results = backtest.run_backtest(json_output=True)
        if results and len(results) >= 2:
            current = results[0]
            best = max(results[1:], key=lambda x: x["projected_pnl"])
            
            log(f"Current P&L: ${current['projected_pnl']:+.2f}", "BACKTEST")
            log(f"Best alternative: {best['name']} ${best['projected_pnl']:+.2f}", "BACKTEST")
            
            improvement = best["projected_pnl"] - current["projected_pnl"]
            if improvement > 0.50:  # At least $0.50 better
                log(f"💡 Backtest recommends: {best['name']} (+${improvement:.2f})", "BACKTEST")
                return {"recommended": best["name"], "config": best["config"], "improvement": improvement}
        
        log("✅ Current config is optimal", "BACKTEST")
        return None
    except Exception as e:
        log(f"⚠️ Backtest failed: {e}", "ERROR")
        return None

def run_phase_incubate():
    """Start incubation if backtest suggested a change."""
    log("🧪 Phase: Incubation check", "PHASE")
    strategies = get_strategies()
    
    # Run backtest to see if we need to incubate
    import importlib.util
    spec = importlib.util.spec_from_file_location("backtest", ROOT / "backtest.py")
    backtest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backtest)
    
    results = backtest.run_backtest(json_output=True)
    if results and len(results) >= 2:
        current = results[0]
        best = max(results[1:], key=lambda x: x["projected_pnl"])
        improvement = best["projected_pnl"] - current["projected_pnl"]
        
        if improvement > 0.50 and len(strategies["incubating"]) < 3:
            start_incubation(strategies, best["name"], best["config"])
            save_strategies(strategies)
            return True
    
    log("No incubation needed", "INCUBATE")
    return False

def run_phase_promote():
    """Check if incubating strategies should be promoted."""
    log("📋 Phase: Promotion check", "PHASE")
    strategies = get_strategies()
    active = strategies["active"]
    
    if not strategies["incubating"]:
        log("No incubating strategies", "PROMOTE")
        return False
    
    # Check if any incubating strategy has enough data
    for inc in strategies["incubating"]:
        if inc["trades"] < 3:
            continue
        
        inc_wr = inc["wins"] / inc["trades"] if inc["trades"] > 0 else 0
        active_wr = active["wins"] / active["trades"] if active["trades"] > 0 else 0
        
        # Promote if incubating has 20%+ better WR and enough trades
        if inc_wr > active_wr * 1.20 and inc["trades"] >= 5:
            log(f"🎯 {inc['name']} WR={inc_wr:.1%} vs active WR={active_wr:.1%}", "PROMOTE")
            promote_strategy(strategies, inc["name"])
            save_strategies(strategies)
            return True
    
    log("No promotion needed", "PROMOTE")
    return False

def run_phase_trade(dry=True):
    """Execute trading with current strategy."""
    strategies = get_strategies()
    active = strategies["active"]
    
    log(f"💹 Phase: Trading ({'DRY' if dry else 'LIVE'}) with '{active['name']}'", "PHASE")
    log(f"   Config: yes_max={active['config']['yes_max']}, tp_roi={active['config']['tp_roi']}, sl_pct={active['config']['sl_pct']}", "PHASE")
    
    # Run the agent with current config
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("agent", ROOT / "agent.py")
        agent = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(agent)
        
        # Override agent config with strategy config
        # This is handled by the agent loading state.json
        # The orchestrator just ensures state.json is in sync
        
        if dry:
            agent.run(dry=True)
        else:
            agent.run(dry=False)
        
        return True
    except Exception as e:
        log(f"⚠️ Trade phase failed: {e}", "ERROR")
        return False

def cycle(live=False):
    """Execute one full orchestrator cycle."""
    opts = get_orchestrator_state()
    opts["total_runs"] += 1
    run_num = opts["total_runs"]
    
    log("=" * 70)
    log(f"🤖 ORCHESTRATOR CYCLE #{run_num} ({'LIVE' if live else 'DRY'})", "CYCLE")
    log(f"   Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 70)
    
    # Load strategies
    strategies = get_strategies()
    
    # Phase 1: Research (every 5 cycles or if requested)
    if run_num % 5 == 1:
        research = run_phase_research()
        opts["last_research"] = run_num
        save_orchestrator_state(opts)
        
        # Update strategy performance from research
        if research:
            s = research.get("summary", {})
            active = strategies["active"]
            if s.get("closed_trades", 0) > 0:
                # Update active strategy stats
                active["trades"] = s.get("closed_trades", active["trades"])
                active["wins"] = s.get("wins", active["wins"])
                active["losses"] = s.get("losses", active["losses"])
                active["pnl"] = s.get("total_pnl", active["pnl"])
                if active["trades"] > 0:
                    active["win_rate"] = active["wins"] / active["trades"]
                save_strategies(strategies)
    else:
        log("⏭️ Skipping research (next at run #" + str((run_num // 5 + 1) * 5) + ")", "PHASE")
    
    # Phase 2: Backtest (every 15 cycles)
    if run_num % 15 == 1 and len(strategies["incubating"]) < 3:
        bt_result = run_phase_backtest()
        opts["last_backtest"] = run_num
        save_orchestrator_state(opts)
        
        if bt_result:
            # Start incubation of recommended config
            start_incubation(strategies, bt_result["recommended"], bt_result["config"])
            save_strategies(strategies)
    else:
        if len(strategies["incubating"]) >= 3:
            log("⏭️ Skipping backtest (max incubating strategies)", "PHASE")
        else:
            log("⏭️ Skipping backtest (next at run #" + str((run_num // 15 + 1) * 15) + ")", "PHASE")
    
    # Phase 3: Incubation check (every 10 cycles)
    if run_num % 10 == 0 or run_num == 1:
        run_phase_incubate()
    
    # Phase 4: Promotion check (every 20 cycles or if enough data)
    if run_num % 20 == 0 or run_num == 1:
        run_phase_promote()
    
    # Phase 5: Trading (every cycle)
    try:
        run_phase_trade(dry=not live)
    except Exception as e:
        log(f"⚠️ Trading failed: {e}", "ERROR")
    
    # Final status
    log("=" * 70)
    strategies = get_strategies()
    active = strategies["active"]
    log(f"📊 STATUS", "STATUS")
    log(f"   Active strategy: {active['name']}", "STATUS")
    log(f"   Trades: {active['trades']} | Wins: {active['wins']} | Losses: {active['losses']}", "STATUS")
    log(f"   Win rate: {active['win_rate']:.1%}", "STATUS")
    log(f"   P&L: ${active['pnl']:+.2f}", "STATUS")
    if strategies["incubating"]:
        log(f"   Incubating: {', '.join(i['name'] for i in strategies['incubating'])}", "STATUS")
    log(f"   Next research: run #{(run_num // 5 + 1) * 5}", "STATUS")
    log(f"   Next backtest: run #{(run_num // 15 + 1) * 15}", "STATUS")
    log(f"   Next promotion check: run #{(run_num // 20 + 1) * 20}", "STATUS")
    log("=" * 70)
    
    opts["phase"] = "trade"
    save_orchestrator_state(opts)

if __name__ == "__main__":
    live = "--live" in sys.argv
    trade_only = "--trade-only" in sys.argv
    
    if "--status" in sys.argv:
        opts = get_orchestrator_state()
        strategies = get_strategies()
        active = strategies["active"]
        print("=" * 60)
        print("🤖 ORCHESTRATOR STATUS")
        print("=" * 60)
        print(f"Total runs: {opts['total_runs']}")
        print(f"Last run: {opts['last_run_at']}")
        print(f"\nActive Strategy: {active['name']}")
        print(f"  Trades: {active['trades']} | W: {active['wins']} | L: {active['losses']}")
        print(f"  Win rate: {active['win_rate']:.1%}")
        print(f"  P&L: ${active['pnl']:+.2f}")
        if strategies["incubating"]:
            print(f"\nIncubating ({len(strategies['incubating'])}):")
            for i in strategies["incubating"]:
                print(f"  - {i['name']} (started: {i['started_at'][:10]})")
        print(f"\nSchedule:")
        print(f"  Research: every 5 cycles")
        print(f"  Backtest: every 15 cycles")
        print(f"  Promotion check: every 20 cycles")
        print("=" * 60)
    else:
        cycle(live=live)
