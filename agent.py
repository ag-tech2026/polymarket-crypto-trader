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
    tp_roi=0.80, sl_pct=0.50, tune_every=10, last_tune_run=0
)

def log(m, lv="INFO"):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{lv}] {m}")
    with open(LOGS / f"agent_{time.strftime('%Y-%m-%d')}.log", "a") as f:
        f.write(f"[{ts}] [{lv}] {m}\n")

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

def jlog(t,s,p):
    l=json.loads(JFILE.read_text()) if JFILE.exists() else []
    l.append(dict(d=datetime.now().isoformat(), t=t, s=s, p=round(p,2)))
    if len(l)>600: l=l[-300:]
    JFILE.write_text(json.dumps(l,indent=2))

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
    
    for sl in list(s["pos"].keys()):
        info=s["pos"][sl]; entry=info["p"]*info["a"]
        match=None
        for x in rp:
            if sl in x["r"]: match=x; break
        if not match:
            log(f"Pending: {sl[:30]}"); continue
            
        pnl=(entry*(1+match["pnl"]/100))-entry; roi=pnl/entry if entry>0 else 0
        
        if roi>=c["tp_roi"]:
            if not dry: bp(["sell",sl,info["o"],f"{info['a']:.2f}","--yes"])
            s["t_pnl"]+=pnl; s["d_pnl"]+=pnl; jlog("tp",sl,pnl); s["pos"].pop(sl)
            log(f"💰 TP: {sl[:30]} ROI={roi:.0%} ${pnl:+.2f}")
        elif roi<=-c["sl_pct"]:
            if not dry: bp(["sell",sl,info["o"],f"{info['a']:.2f}","--yes"])
            s["t_pnl"]+=pnl; s["d_pnl"]+=pnl; jlog("sl",sl,pnl); s["pos"].pop(sl)
            log(f"🛑 SL: {sl[:30]} ROI={roi:.0%} ${pnl:+.2f}")

    # 3. Self-Tune
    tune(s)

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
                    roi=round((1-yp)/yp,2); mkts.append(dict(s=mk["slug"],o="Yes",p=yp,r=roi,q=mk.get("question","")))
                elif yp>=0.85 and 0.05<=np_<=0.20:
                    roi=round((1-np_)/np_,2); mkts.append(dict(s=mk["slug"],o="No",p=np_,r=roi,q=mk.get("question","")))
    
    mkts.sort(key=lambda x:-x["r"])
    ex=set(s["pos"].keys())
    av=[m for m in mkts if m["s"] not in ex]
    log(f"👁️ Scanned {len(mkts)}, Found {len(av)} opportunities")
    
    # 5. Execute
    slots=c["max_pos"]-len(s["pos"])
    for m in av[:slots]:
        a=sz(s); q_short=m['q'][:30].replace('"','')
        if not dry:
            d_r,err=bp(["buy",m["s"],m["o"],f"{a:.2f}","--yes","--output","json"])
            if err: 
                log(f"❌ Buy failed: {err}","WARN")
            else:
                s["pos"][m["s"]]=dict(o=m["o"],p=m["p"],a=a)
                jlog("open",m["s"],0)
                log(f"✅ LIVE {m['o']}@{m['p']:.0%} ${a:.2f} → {q_short}")
        else:
            s["pos"][m["s"]]=dict(o=m["o"],p=m["p"],a=a)
            jlog("open",m["s"],0)
            log(f"✅ DRY {m['o']}@{m['p']:.0%} ${a:.2f} → {q_short}")
            
    log(f"📊 {len(s['pos'])}/{c['max_pos']} pos | ${s['t_pnl']:+.2f}")
    log("="*60); save(s)

if __name__=="__main__": 
    if "--live" in sys.argv:
        run(dry=False)
    else:
        run(dry=True)