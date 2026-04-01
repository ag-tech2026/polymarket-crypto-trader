#!/usr/bin/env python3
"""Polymarket Autonomous Agent v3 — self-tuning, zero manual edits."""
import json, os, subprocess, sys, time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
LOGS  = ROOT / "data/logs"
JOURN = ROOT / "data/journals"
for d in [STATE, LOGS, JOURN]: d.mkdir(parents=True, exist_ok=True)

SFILE = STATE / "state.json"
JFILE = JOURN / "strategy_journal.json"

# Default config
CFG = dict(
    max_pos=5, trade=1.0, max_trade=2.5, min_trade=0.5,
    daily_lim=3.0, cap=2.0, yes_min=0.05, yes_max=0.40,
    tp_roi=0.80, sl_pct=0.50, tune_every=10, last_tune_run=0,
    fast_mode=False,                  # Tighter TP/SL for faster learning
    max_per_category=2,               # Correlation limit per market category
    telegram_alerts=True              # Send TP/SL alerts to Telegram
)

def log(m, lv="INFO"):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{lv}] {m}")
    with open(LOGS / f"agent_{time.strftime('%Y-%m-%d')}.log", "a") as f:
        f.write(f"[{ts}] [{lv}] {m}\n")

def telegram_alert(msg):
    """Send a Telegram alert."""
    if not CFG.get("telegram_alerts", True):
        return
    token = "8790751627:AAFytj-a3W3OegqSsYNAtjIVenTHKFfR55s"
    chat_id = "7372567737"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = f"chat_id={chat_id}&text={msg}&parse_mode=HTML"
    try:
        subprocess.run(["curl", "-s", "-X", "POST", url, "-d", payload],
                       capture_output=True, text=True, timeout=5)
    except:
        pass

def bp(a, t=30):
    r=subprocess.run(["bullpen","polymarket"]+a, capture_output=True, text=True, timeout=t)
    if r.returncode!=0: return None, r.stderr[:300]
    try: return json.loads(r.stdout), None
    except: return r.stdout.strip(), None

def load():
    if SFILE.exists():
        try:
            raw=json.loads(SFILE.read_text())
            # Map old keys to new schema
            s={}
            for k,v in CFG.items(): s[k]=raw.get(k,v)
            
            # Convert positions: old "outcome"/"price"/"amount" → new "o"/"p"/"a"
            raw_pos=raw.get("positions",raw.get("pos",{}))
            s_pos={}
            for sl,info in raw_pos.items():
                if "o" in info:
                    s_pos[sl]=info
                else:
                    s_pos[sl]=dict(
                        o=info.get("outcome","Yes"),
                        p=info.get("price",0.5),
                        a=info.get("amount",1.0),
                    )
            s["pos"]=s_pos
            
            s["runs"]=raw.get("runs",raw.get("cnt",0))
            s["t_pnl"]=raw.get("total_pnl",raw.get("t_pnl",0.0))
            s["d_pnl"]=raw.get("daily_pnl",raw.get("d_pnl",0.0))
            s["lr"]=raw.get("last_run",raw.get("lr",""))
            return s
        except: pass
    return {**CFG, "pos":{}, "runs":0, "t_pnl":0.0, "d_pnl":0.0, "lr":""}

def save(s):
    s["lr"]=time.strftime("%Y-%m-%dT%H:%M:%S")
    SFILE.write_text(json.dumps(s,indent=2))

def jlog(t, s, p, **meta):
    """Enhanced journaling with market metadata for research."""
    l=json.loads(JFILE.read_text()) if JFILE.exists() else []
    entry = dict(d=datetime.now().isoformat(), t=t, s=s, p=round(p,2), **meta)
    l.append(entry)
    if len(l)>600: l=l[-300:]
    JFILE.write_text(json.dumps(l,indent=2))

def _categorize(text):
    """Classify a market question into a research category."""
    t = text.lower()
    if any(w in t for w in ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "doge", "bnb", "xrp", "crypto", "altcoin"]):
        return "crypto"
    if any(w in t for w in ["election", "vote", "politic", "president", "congress", "governor"]):
        return "politics"
    if any(w in t for w in ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto"]):
        return "crypto"
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

def sz(s):
    p=s["t_pnl"]
    if p>=0: return round(min(s["max_trade"], s["trade"]+int(abs(p)/3)*0.15),2)
    return round(max(s["min_trade"], s["trade"]-int(abs(p)/3)*0.15),2)

def tune(s):
    """Auto-tune YES buy zone based on journal history."""
    if not JFILE.exists(): return
    jl=json.loads(JFILE.read_text())
    cl=[x for x in jl if x["t"] in("tp","sl")]
    if len(cl)<5: return
    if s["runs"]-s.get("last_tune_run",0)<s.get("tune_every",10): return
    
    recent=cl[-50:]; wins=sum(1 for x in recent if x["t"]=="tp")
    n=len(recent); wr=wins/n if n else 0.5; ch=False
    
    if wr>=0.60:
        s["yes_max"]=round(min(0.55,s.get("yes_max",0.40)+0.05),2)
        log(f"🎯 WinRate({wr:.0%}): Widening YES to {s['yes_max']:.0%}"); ch=True
    elif wr<0.40:
        s["yes_max"]=round(max(0.10,s.get("yes_max",0.40)-0.05),2)
        log(f"📉 WinRate({wr:.0%}): Tightening YES to {s['yes_max']:.0%}"); ch=True
        
    if ch: s["last_tune_run"]=s["runs"]

def run(dry=True):
    mode="LIVE" if not dry else "DRY"
    s=load(); s["runs"]+=1; c=s
    size=sz(s)
    log("="*60); log(f"[{mode}] #{s['runs']} sz:${size:.2f} YES<={c['yes_max']:.0%}")

    # Fast mode: override TP/SL for faster learning
    if c.get("fast_mode"):
        orig_tp=c["tp_roi"]; orig_sl=c["sl_pct"]
        c["tp_roi"]=0.50; c["sl_pct"]=0.30
        log(f"⚡ Fast mode: TP={c['tp_roi']:.0%} SL={c['sl_pct']:.0%}")
    
    # 1. Safety Check
    bal=None; d,e=bp(["clob","balance"])
    if not e and str(d).strip():
        for l in str(d).split("\n"):
            if "Balance:" in l:
                try: bal=float(l.split("$")[1].split(",")[0]); break
                except: pass
    if bal is not None:
        log(f"💰${bal:.2f} | Day:${s['d_pnl']:+.2f} | Tot:${s['t_pnl']:+.2f}")
        if bal<c["cap"]: log("🛑 Balance below cap","CRIT"); save(s); return
        if s["d_pnl"]<=-c["daily_lim"]: log("🛑 Daily limit hit","CRIT"); save(s); return
    else: log("⚠️ Balance check failed"); save(s); return

    # 2. Manage Exits
    rp=[]; d2,e2=bp(["positions"])
    if not e2 and str(d2).strip():
        for l in str(d2).split("\n"):
            if "$" in l and "%" in l:
                for x in reversed(l.split()):
                    if "%" in x:
                        try: rp.append(dict(r=l, pnl=float(x.replace("%","")))); break
                        except: pass
    
    def _match_pos(slug, text):
      import re as _re
      text_clean = text.lower()
      text_clean = _re.sub(r"[$,\.]", "", text_clean)
      slug_parts = slug.replace("-"," ").split()
      sig = [w for w in slug_parts if len(w)>2 and w not in ("will","the","and","for","with","reach","above","below","after","before","between","december","november","october","september","january","february","by")]
      if not sig:
        return False
      words_found = 0
      for w in sig:
        if w in text_clean or (w.isdigit() and len(w)>3 and str(int(w)) in text_clean):
          words_found += 1
      # Threshold: 2 for short slugs (3-6 sig words), 3 for longer ones
      # This handles Bullpen's ~40-char truncation cutting off year endings
      needed = min(3, max(2, (len(sig) + 1) // 3))
      return words_found >= needed


    for sl in list(s["pos"].keys()):
        info=s["pos"][sl]; entry=info["p"]*info["a"]
        match=None
        for x in rp:
            if _match_pos(sl, x["r"]): match=x; break
        if not match:
            log(f"⏳ Pending (unmatched): {sl[:40]}"); continue
            
        pnl=(entry*(1+match["pnl"]/100))-entry; roi=pnl/entry if entry>0 else 0
        
        if roi>=c["tp_roi"]:
            if not dry: bp(["sell",sl,info["o"],f"{info['a']:.2f}","--yes"])
            s["t_pnl"]+=pnl; s["d_pnl"]+=pnl
            meta={k:v for k,v in info.items() if k in("cat","pr","e","liq","vol") and v}
            jlog("tp",sl,pnl, **meta); s["pos"].pop(sl)
            log(f"💰 TP: {sl[:30]} ROI={roi:.0%} ${pnl:+.2f}")
            msg = f"💰 TP hit: {sl[:40]}\nROI: {roi:.0%} | P&L: ${pnl:+.2f}\nRun #{s['runs']}"
            if c.get("telegram_alerts"): telegram_alert(msg)
        elif roi<=-c["sl_pct"]:
            if not dry: bp(["sell",sl,info["o"],f"{info['a']:.2f}","--yes"])
            s["t_pnl"]+=pnl; s["d_pnl"]+=pnl
            meta={k:v for k,v in info.items() if k in("cat","pr","e","liq","vol") and v}
            jlog("sl",sl,pnl, **meta); s["pos"].pop(sl)
            log(f"🛑 SL: {sl[:30]} ROI={roi:.0%} ${pnl:+.2f}")
            msg = f"🛑 SL hit: {sl[:40]}\nROI: {roi:.0%} | P&L: ${pnl:+.2f}\nRun #{s['runs']}"
            if c.get("telegram_alerts"): telegram_alert(msg)

    # 3. Self-Tune
    tune(s)

    # 3b. Research (every 5 runs, or on --research flag)
    if s["runs"] % 5 == 0 or "--research" in sys.argv:
        try:
            log("🔬 Running research analysis...")
            import importlib.util
            spec = importlib.util.spec_from_file_location("research", ROOT / "research.py")
            research_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(research_mod)
            r = research_mod.analyze()
            research_mod.save_research(r)
            if r.get("recommendations"):
                for rec in r["recommendations"]:
                    log(f"  {rec}", "RESEARCH")
        except Exception as e:
            log(f"⚠️ Research failed: {e}", "WARN")

    # 3c. Backtest (every 15 runs, or on --backtest flag)
    if s["runs"] % 15 == 0 or "--backtest" in sys.argv:
        try:
            log("📊 Running backtest comparison...")
            import importlib.util
            spec = importlib.util.spec_from_file_location("backtest", ROOT / "backtest.py")
            backtest_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(backtest_mod)
            results = backtest_mod.run_backtest(json_output=True)
            if results:
                best = max(results, key=lambda x: x["projected_pnl"])
                current = results[0]
                if best["projected_pnl"] > current["projected_pnl"] * 1.2:
                    log(f"🎯 Backtest suggests: {best['name']} (${best['projected_pnl']:+.2f} vs ${current['projected_pnl']:+.2f})", "BACKTEST")
                else:
                    log("✅ Backtest: current config is competitive", "BACKTEST")
        except Exception as e:
            log(f"⚠️ Backtest failed: {e}", "WARN")

    # 4. Discover & Score
    mkts=[]
    scopes = ["crypto"]
    for sc in scopes:
        d,e=bp(["discover",sc,"--min-liquidity","100000","--sort","volume","--limit","40","--output","json"])
        if e: continue
        if isinstance(d,str):
            try: d=json.loads(d)
            except: continue
        for ev in d.get("events",[]):
            for mk in ev.get("markets",[]):
                if mk.get("closed"): continue
                outs=mk.get("outcomes",[])
                if len(outs)<2: continue
                yp=outs[0].get("price") or 0; np_=outs[1].get("price") or 0
                if not yp or not np_: continue
                
                if c["yes_min"]<=yp<=c["yes_max"]:
                    roi=round((1-yp)/yp,2)
                    pr="t1" if yp<=0.10 else "t2" if yp<=0.20 else "t3" if yp<=0.30 else "t4"
                    cat=_categorize(ev.get("title","")+mk.get("question",""))
                    ed=ev.get("end_date","")
                    mkts.append(dict(s=mk["slug"],o="Yes",p=yp,r=roi,q=mk.get("question",""),
                                     liq=mk.get("liquidity",0), vol=mk.get("volume_24h",0),
                                     cat=cat, e=ed[:10] if ed else "TBD", pr=pr))
                elif yp>=0.85 and 0.05<=np_<=0.20:
                    roi=round((1-np_)/np_,2)
                    cat=_categorize(ev.get("title","")+mk.get("question",""))
                    ed=ev.get("end_date","")
                    mkts.append(dict(s=mk["slug"],o="No",p=np_,r=roi,q=mk.get("question",""),
                                     liq=mk.get("liquidity",0), vol=mk.get("volume_24h",0),
                                     cat=cat, e=ed[:10] if ed else "TBD", pr="t1"))
    
    mkts.sort(key=lambda x:-x["r"])
    ex=set(s["pos"].keys())
    av=[m for m in mkts if m["s"] not in ex]
    log(f"👁️ Scanned {len(mkts)}, Found {len(av)} opportunities")
    
    # 5. Execute with correlation limits
    slots=c["max_pos"]-len(s["pos"])
    max_per_cat = c.get("max_per_category", 2)
    
    # Count current positions per category
    cat_counts = {}
    for sl, info in s["pos"].items():
        cat = info.get("cat", "crypto")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    
    filled = 0
    for m in av[:slots]:
        cat = m.get("cat", "crypto")
        # Correlation limit check
        if cat_counts.get(cat, 0) >= max_per_cat:
            log(f"⏭️ Skip {m['s'][:30]}: {cat} cap reached ({cat_counts[cat]}/{max_per_cat})")
            continue
        
        a=sz(s); q_short=m['q'][:30].replace('"','')
        meta = {
            "cat": m.get("cat", "crypto"),     # market category
            "e": m.get("e", ""),                # end date/timeframe
            "liq": m.get("liq", 0),             # liquidity
            "vol": m.get("vol", 0),             # volume
            "pr": m.get("pr", ""),              # price bucket
        }
        if not dry:
            d_r,err=bp(["buy",m["s"],m["o"],f"{a:.2f}","--yes","--output","json"])
            if err: 
                log(f"❌ Buy failed: {err}","WARN")
            else:
                s["pos"][m["s"]]=dict(o=m["o"],p=m["p"],a=a, **{k:v for k,v in meta.items() if v})
                jlog("open",m["s"],0, **{k:v for k,v in meta.items() if v})
                log(f"✅ LIVE {m['o']}@{m['p']:.0%} ${a:.2f} → {q_short}")
                filled += 1
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
        else:
            # DRY: log opportunity but don't persist to state (prevents zombie positions)
            jlog("dry_open",m["s"],0, **{k:v for k,v in meta.items() if v})
            log(f"✅ DRY {m['o']}@{m['p']:.0%} ${a:.2f} → {q_short}")
            filled += 1
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            
    log(f"📊 {len(s['pos'])}/{c['max_pos']} pos | ${s['t_pnl']:+.2f} | Filled {filled} trades")
    log("="*60); save(s)

if __name__=="__main__": 
    if "--research" in sys.argv:
        # Run research standalone
        import importlib.util
        spec = importlib.util.spec_from_file_location("research", ROOT / "research.py")
        research_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(research_mod)
        r = research_mod.analyze()
        research_mod.save_research(r)
        research_mod.print_report(r)
    elif "--backtest" in sys.argv:
        # Run backtest standalone
        import importlib.util
        spec = importlib.util.spec_from_file_location("backtest", ROOT / "backtest.py")
        backtest_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backtest_mod)
        backtest_mod.run_backtest()
    elif "--live" in sys.argv:
        run(dry=False)
    else:
        run(dry=True)