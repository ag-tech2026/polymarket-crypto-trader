#!/usr/bin/env python3
"""
Ensemble Scoring Engine — 4-pillar confidence system for trade decisions.

Pillars:
  1. Sentiment: Research-backed signal strength (from research.py)
  2. Historical Edge: Backtest performance on similar setups
  3. Market Dynamics: Liquidity, volume, price momentum
  4. Portfolio Health: Risk budget, correlation, exposure time

Each pillar scores 0-10. Sum determines action:
  <25 → Skip (weak signal)
  25-30 → Execute $1 (base trade)
  >30 → Execute $1 + "High Conviction" flag
"""
import json, math
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
JFILE = ROOT / "data" / "journals" / "strategy_journal.json"
RFILE = STATE / "research.json"


def load_journal():
    if not JFILE.exists():
        return []
    with open(JFILE) as f:
        return json.load(f)


def load_research():
    if not RFILE.exists():
        return {}
    with open(RFILE) as f:
        return json.load(f)


def get_sentiment_score(market, research=None):
    """
    Pillar 1: Sentiment score based on research analysis.
    
    Looks at research recommendations for this market's category and price bucket.
    Score 0-10 based on historical research confidence.
    """
    if research is None:
        research = load_research()
    
    cat = market.get("cat", "general")
    pr = market.get("pr", "t1")
    
    score = 5.0  # Neutral baseline (no research data yet)
    
    # Category boost/cut
    by_cat = research.get("by_category", {})
    if cat in by_cat:
        cs = by_cat[cat]
        wr = cs.get("win_rate", 0.5)
        trades = cs.get("trades", 0)
        rec = cs.get("recommendation", "neutral")
        
        if rec == "focus" and trades >= 3:
            score += 2.0
        elif rec == "avoid" and trades >= 3:
            score -= 2.5
        else:
            # Scale linearly between 0.35-0.65 win rate
            score += (wr - 0.5) * 6  # ±1.5 points
    
    # Price bucket boost/cut
    by_bucket = research.get("by_price_bucket", {})
    if pr in by_bucket:
        bs = by_bucket[pr]
        wr = bs.get("win_rate", 0.5)
        trades = bs.get("trades", 0)
        rec = bs.get("recommendation", "hold")
        
        if rec == "boost" and trades >= 3:
            score += 1.5
        elif rec == "cut" and trades >= 3:
            score -= 2.0
        else:
            score += (wr - 0.5) * 3  # ±0.75 points
    
    # Outcome bias (if available)
    by_outcome = research.get("by_outcome", {})
    outcome = market.get("o", "Yes")
    if outcome in by_outcome:
        os_ = by_outcome[outcome]
        wr = os_.get("win_rate", 0.5)
        total = os_.get("total", 0)
        if total >= 3:
            score += (wr - 0.5) * 2  # ±0.5 points
    
    # Clamp 0-10
    return max(0, min(10, round(score, 1)))


def get_historical_edge_score(market, journal=None):
    """
    Pillar 2: Historical Edge score from recent trades.
    
    Looks at the last 20 closed trades of the same category.
    Penalizes if recent 5-trade rolling average is weak.
    Score 0-10 based on empirical win rate.
    """
    if journal is None:
        journal = load_journal()
    
    cat = market.get("cat", "general")
    pr = market.get("pr", "t1")
    
    # Closed trades only
    closed = [x for x in journal if x.get("t") in ("tp", "sl")]
    if len(closed) < 3:
        return 5.0  # Not enough data, neutral
    
    # Filter by category (fallback to all if too few)
    cat_trades = [t for t in closed if t.get("cat") == cat]
    if len(cat_trades) < 3:
        cat_trades = closed[:20]  # Use general pool
    
    # Recent 20 trades
    recent = cat_trades[-20:]
    wins = sum(1 for t in recent if t["t"] == "tp")
    total = len(recent)
    wr = wins / total if total > 0 else 0.5
    
    # Recent 5-trade rolling average
    last5 = cat_trades[-5:]
    if len(last5) >= 3:
        wins5 = sum(1 for t in last5 if t["t"] == "tp")
        wr5 = wins5 / len(last5)
    else:
        wr5 = wr
    
    # Base score from overall win rate
    score = wr * 10  # 0-10 scale
    
    # Penalize if recent 5-trade average is weak (momentum against us)
    if wr5 < 0.40:
        score -= 2.0
    elif wr5 < 0.30:
        score -= 3.0
    
    # Bonus for consistent category performance
    if wr >= 0.60 and total >= 5:
        score += 1.0
    
    return max(0, min(10, round(score, 1)))


def get_market_dynamics_score(market):
    """
    Pillar 3: Market Dynamics score from liquidity and volume.
    
    - Liquidity depth (can we enter/exit without slippage?)
    - Volume activity (is there enough interest?)
    - Price position relative to fair value
    
    Score 0-10. Higher = healthier market conditions.
    """
    liq = market.get("liq", 0)
    vol = market.get("vol", 0)
    price = market.get("p", 0.5)
    outcome = market.get("o", "Yes")
    
    score = 5.0  # Neutral baseline
    
    # Liquidity score (0-3 points)
    # Polymarket markets: $100K+ is liquid, $1M+ is very liquid
    if liq >= 1000000:
        score += 3.0
    elif liq >= 500000:
        score += 2.0
    elif liq >= 100000:
        score += 1.0
    elif liq >= 50000:
        score += 0.5
    else:
        score -= 2.0  # Illiquid markets are risky
    
    # Volume score (0-3 points)
    # Volume shows active interest
    if vol >= 500000:
        score += 3.0
    elif vol >= 100000:
        score += 2.0
    elif vol >= 50000:
        score += 1.0
    elif vol >= 10000:
        score += 0.5
    else:
        score -= 1.0  # Dead market
    
    # Price position analysis
    # Buying Yes at 5-10¢ = high expected return but low probability
    # Buying Yes at 30-40¢ = moderate return, better probability
    # Optimal zone: 15-35¢ for our strategy
    if 0.15 <= price <= 0.35:
        score += 1.0  # Sweet spot
    elif 0.05 <= price <= 0.50:
        score += 0.5  # Acceptable
    elif price < 0.05:
        score -= 1.5  # Too cheap, probably going to zero
    elif price > 0.50:
        score -= 2.0  # We don't trade expensive Yes positions
    
    # Time to resolution bonus
    # Markets resolving in 1-7 days = optimal (fast turnover, enough movement)
    days_to_res = market.get("days_to_res")
    if days_to_res is not None and days_to_res >= 0:
        if 1 <= days_to_res <= 3:
            score += 2.0  # Fast resolution, excellent capital turnover
        elif 4 <= days_to_res <= 7:
            score += 1.0  # Still good
        elif 8 <= days_to_res <= 14:
            score += 0.0  # Neutral
        elif days_to_res > 14:
            score -= 1.0  # Too far out, capital tied up
    elif ed and ed != "TBD":
        pass  # Fallback for when days_to_res wasn't pre-computed
    
    return max(0, min(10, round(score, 1)))


def get_portfolio_health_score(market, state):
    """
    Pillar 4: Portfolio Health score.
    
    - Daily loss proximity (close to $3 limit = 0 score)
    - Correlation check (too many in same category = penalty)
    - Position count (approaching 5 max = reduced scoring)
    - Recent P&L trajectory
    
    Score 0-10. Hard block at 0 if daily limit near.
    """
    d_pnl = state.get("d_pnl", 0.0)
    daily_lim = state.get("daily_lim", 3.0)
    max_pos = state.get("max_pos", 5)
    current_pos = len(state.get("pos", {}))
    cat = market.get("cat", "general")
    max_per_cat = state.get("max_per_category", 2)
    
    # Count current positions per category
    cat_counts = {}
    for sl, info in state.get("pos", {}).items():
        c = info.get("cat", "general")
        cat_counts[c] = cat_counts.get(c, 0) + 1
    
    # Hard block: daily limit within 30% remaining
    remaining_daily = daily_lim + d_pnl  # how much more we can lose
    if remaining_daily <= daily_lim * 0.3:
        return 0.0  # Hard block — we're too close to daily limit
    
    score = 7.0  # Start high (healthy portfolio by default)
    
    # Daily P&L proximity penalty (0-3 points)
    if d_pnl >= 0:
        score += 2.0  # We're profitable today, green light
    else:
        loss_ratio = abs(d_pnl) / daily_lim
        if loss_ratio > 0.7:
            score -= 3.0  # Very close to daily limit
        elif loss_ratio > 0.5:
            score -= 2.0
        elif loss_ratio > 0.3:
            score -= 1.0
    
    # Correlation penalty (0-3 points)
    current_cat_count = cat_counts.get(cat, 0)
    if current_cat_count >= max_per_cat:
        score -= 3.0  # Category cap would be exceeded
    elif current_cat_count == max_per_cat - 1:
        score -= 1.5  # One more would hit cap
    
    # Position count pressure (0-2 points)
    if current_pos >= max_pos - 1:
        score -= 2.0  # Nearly full, only take A+ trades
    elif current_pos >= max_pos - 2:
        score -= 0.5  # Getting full
    
    # Total P&L trajectory bonus
    t_pnl = state.get("t_pnl", 0.0)
    if t_pnl > 1.0:
        score += 0.5  # Overall profitable, confidence boost
    
    return max(0, min(10, round(score, 1)))


def score_market(market, state, research=None, journal=None):
    """
    Score a market opportunity across all 4 pillars.
    
    Returns dict with individual scores and total.
    """
    sentiment = get_sentiment_score(market, research)
    edge = get_historical_edge_score(market, journal)
    dynamics = get_market_dynamics_score(market)
    portfolio = get_portfolio_health_score(market, state)
    
    total = sentiment + edge + dynamics + portfolio
    
    # Determine conviction level
    if total >= 30:
        conviction = "HIGH"
    elif total >= 25:
        conviction = "MEDIUM"
    else:
        conviction = "LOW"
    
    return {
        "sentiment": sentiment,
        "historical_edge": edge,
        "market_dynamics": dynamics,
        "portfolio_health": portfolio,
        "total": round(total, 1),
        "conviction": conviction,
    }


def should_trade(score_result, min_threshold=25):
    """
    Decision rule based on ensemble score.
    
    Returns: (should_trade: bool, confidence: str)
    """
    total = score_result["total"]
    
    if total >= min_threshold:
        confidence = "HIGH" if total >= 30 else "MEDIUM"
        return True, confidence
    return False, "SKIP"
