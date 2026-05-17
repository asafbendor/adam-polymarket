"""
Seed script - populates agents' memory from:
1. Known learnings from the first week of operation (hardcoded)
2. Optional Railway log file passed as argument

Usage:
  python seed_memory.py                    # seed known learnings only
  python seed_memory.py railway_log.txt   # also parse log file
"""
import sys
import re
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

import db
import memory
import state

def ts():
    return datetime.now(timezone.utc).isoformat()


# ── 1. Seed known learnings from week 1 ──────────────────────────────

SCOUT_LEARNINGS = {
    "btc_price_may_2026": (
        "BTC trading $79k-$104k range in May 2026. "
        "Markets asking 'dip to $70k?' at 14.5% are likely overpriced (need -12% drop). "
        "Markets asking 'hit $150k?' at 35% need +90% gain - strong NO bet. "
        "Use Binance to verify current price before every crypto bet."
    ),
    "crypto_direction_logic": (
        "CRITICAL: 'Will BTC dip/fall/crash to $X?' = BET NO if current >> X (big drop needed). "
        "CRITICAL: 'Will BTC reach/hit/exceed $X?' = BET NO if current << X (big gain needed). "
        "Bug history: inverted logic caused YES bets on $30k dip when BTC at $79k - all lost."
    ),
    "geopolitical_lesson": (
        "US-Iran peace deal bets (May 15, May 31 deadlines): placed YES at 0.65%-11.85%. "
        "Result: likely losses. Lesson: geopolitical markets need real-time news verification. "
        "Without live news API, avoid short-deadline political bets. "
        "Longer timelines (June 30+) are less risky but still speculative."
    ),
    "eurovision_2026_lesson": (
        "Eurovision 2026: bet YES on Bulgaria (0.0242), Italy (0.0191), Australia (0.1025). "
        "Eurovision requires domain expertise on entries, song quality, voting blocs. "
        "Small-price YES bets on multiple countries = lottery tickets, not edge bets. "
        "Better: bet NO on overpriced favorites, or skip Eurovision entirely."
    ),
    "polymarket_calibration": (
        "Polymarket markets are reasonably efficient for major events. "
        "Look for mispricings at extremes: <5% or >85% where edge exists. "
        "Crypto markets with 12%+ margin from target offer real edge. "
        "Political markets with <48h to deadline are noise - avoid."
    ),
    "binance_fallback_works": (
        "When Claude Scout returns 0 opportunities, Binance price fallback finds crypto bets. "
        "Best opportunities: BTC/ETH targets 12%+ away from current price. "
        "Extend market window to 30 days - most crypto markets resolve in weeks not days."
    ),
    "market_window_insight": (
        "7-day window too restrictive - most liquid crypto markets resolve in 30-60 days. "
        "Switch to 30-day window to find enough opportunities. "
        "Short-window (<7 days) favors speculative events, not data-backed bets."
    ),
}

TRADER_LEARNINGS = {
    "working_config": (
        "WORKING: Hybrid approach - old py-clob-client 0.34.6 for API credentials, "
        "py_clob_client_v2 for order signing (EIP-712 version 2). "
        "signature_type=2 for proxy wallet in both clients."
    ),
    "order_version_history": (
        "Polymarket migrated to CLOB V2 on April 28 2026. "
        "Old py-clob-client alone: order_version_mismatch (EIP-712 version 1 vs 2). "
        "V2 client alone: failed credentials. "
        "Solution: old client for auth + V2 client for signing = works."
    ),
    "order_errors_history": (
        "Errors encountered and resolved: "
        "1. order_version_mismatch -> switched to V2 signing. "
        "2. dict has no attribute tick_size -> use SimpleNamespace for options. "
        "3. API credentials needed -> old client create_or_derive_api_creds() works. "
        "4. invalid signature -> ensure sig_type=2 for proxy wallet."
    ),
    "minimum_order_size": (
        "Polymarket minimum order size constraints exist on some markets. "
        "For low-price tokens (<$0.05), may need more shares to meet minimum. "
        "Calculate: shares = ceil(bet_size / limit_price * 100) / 100, ensure price*shares >= $1."
    ),
}

KNOWN_OUTCOMES = [
    # Bulgaria Eurovision - likely resolved May 16
    {
        "question": "Will Bulgaria win Eurovision 2026?",
        "direction": "YES", "estimated_prob": 0.05, "market_price": 0.0242,
        "outcome": "unknown", "pnl": None,
        "reason": "Strong finalist; market at 2.35% vs realistic 4-5%",
    },
    {
        "question": "Will US-Iran permanent peace deal happen by May 15 2026?",
        "direction": "YES", "estimated_prob": 0.18, "market_price": 0.0067,
        "outcome": "lost", "pnl": -1.5,
        "reason": "Trump-Xi summit context; 13h deadline was too short",
    },
]


def seed_known():
    print("Seeding Scout learnings...")
    for key, value in SCOUT_LEARNINGS.items():
        memory.remember("scout", key, value)
        print(f"  Scout: {key}")

    print("Seeding Trader learnings...")
    for key, value in TRADER_LEARNINGS.items():
        memory.remember("trader", key, value)
        print(f"  Trader: {key}")

    print("Seeding known outcomes...")
    for o in KNOWN_OUTCOMES:
        if o["pnl"] is not None:
            memory.record_outcome(
                o["question"], o["direction"], o["estimated_prob"],
                o["market_price"], o["outcome"], o["pnl"], o["reason"]
            )
            print(f"  Outcome: {o['question'][:50]} -> {o['outcome']}")


# ── 2. Parse Railway log file ─────────────────────────────────────────

def parse_log(path: str):
    print(f"\nParsing log file: {path}")
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    # Extract bet placements
    bet_pattern = re.compile(
        r"Bet placed.*?question.*?[:=]\s*['\"]?([^'\"\\n]+)['\"]?.*?"
        r"direction.*?[:=]\s*['\"]?(YES|NO)['\"]?.*?"
        r"est.*?[:=]\s*([\d.]+).*?"
        r"edge.*?[:=]\s*([\d.]+)",
        re.IGNORECASE | re.DOTALL
    )
    bets_found = bet_pattern.findall(content)

    # Extract learnings stored by agents
    learning_pattern = re.compile(
        r"remember_learning.*?key.*?['\"]([^'\"]+)['\"].*?value.*?['\"]([^'\"]{20,})['\"]",
        re.IGNORECASE | re.DOTALL
    )
    learnings = learning_pattern.findall(content)
    for key, value in learnings:
        memory.remember("scout", f"log_{key}", value[:500])
        print(f"  Extracted learning: {key}")

    # Extract errors for Trader
    error_pattern = re.compile(r"Order failed.*?:\s*(.+?)(?:\n|$)", re.IGNORECASE)
    errors = error_pattern.findall(content)
    unique_errors = list(set(errors))[:10]
    for i, err in enumerate(unique_errors):
        memory.remember("trader", f"log_error_{i}", err[:300])
        print(f"  Extracted error: {err[:60]}")

    # Extract win/loss outcomes
    win_pattern = re.compile(r"WIN.*?P&L.*?\+([\d.]+).*?question.*?[:=]\s*['\"]?([^'\"\\n]+)", re.IGNORECASE)
    loss_pattern = re.compile(r"LOSS.*?P&L.*?-([\d.]+).*?question.*?[:=]\s*['\"]?([^'\"\\n]+)", re.IGNORECASE)

    for match in win_pattern.finditer(content):
        pnl, question = float(match.group(1)), match.group(2)[:100]
        memory.record_outcome(question, "YES", 0.5, 0.5, "won", pnl, "from log")
        print(f"  Win extracted: {question[:50]} +${pnl}")

    for match in loss_pattern.finditer(content):
        pnl, question = float(match.group(1)), match.group(2)[:100]
        memory.record_outcome(question, "YES", 0.5, 0.5, "lost", -pnl, "from log")
        print(f"  Loss extracted: {question[:50]} -${pnl}")

    print(f"Log parsing complete. Found {len(bets_found)} bets, {len(learnings)} learnings, {len(unique_errors)} errors.")


# ── Main ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Adam Memory Seeder ===")
    memory.init()
    state.init()

    seed_known()

    if len(sys.argv) > 1:
        log_path = sys.argv[1]
        if os.path.exists(log_path):
            parse_log(log_path)
        else:
            print(f"Log file not found: {log_path}")

    print("\n✓ Memory seeded successfully.")
    print(f"  DB type: {'PostgreSQL' if db.USE_PG else 'SQLite'}")

    # Show summary
    scout_mem = memory.recall_all("scout")
    trader_mem = memory.recall_all("trader")
    outcomes = memory.get_outcomes(5)
    print(f"  Scout learnings: {len(scout_mem)}")
    print(f"  Trader fixes: {len(trader_mem)}")
    print(f"  Past outcomes: {len(outcomes)}")
