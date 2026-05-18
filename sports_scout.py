"""
Sports Scout - finds Polymarket sports betting opportunities using ESPN free API.
No API key needed, no cost.

ESPN gives real win probabilities for NBA, NFL, MLB, NHL, soccer games.
We compare ESPN probability to Polymarket price to find edge.
"""
import asyncio
import logging
import re
from typing import Optional

import aiohttp

logger = logging.getLogger("sports_scout")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# Sport/league combinations to check
LEAGUES = [
    ("basketball", "nba"),
    ("football",   "nfl"),
    ("baseball",   "mlb"),
    ("hockey",     "nhl"),
    ("soccer",     "usa.1"),       # MLS
    ("soccer",     "eng.1"),       # Premier League
    ("soccer",     "esp.1"),       # La Liga
    ("soccer",     "ger.1"),       # Bundesliga
    ("soccer",     "ita.1"),       # Serie A
    ("basketball", "mens-college-basketball"),
]

MIN_EDGE = 0.07   # 7% minimum edge vs market price


def _name_overlap(a: str, b: str) -> float:
    """Word overlap between two team/player names."""
    stop = {"will", "the", "a", "an", "vs", "at", "in", "on", "win", "beat",
            "defeat", "game", "match", "tonight", "today", "fc", "united", "city"}
    wa = {w.lower() for w in re.findall(r"[a-zA-Z]+", a) if w.lower() not in stop and len(w) > 2}
    wb = {w.lower() for w in re.findall(r"[a-zA-Z]+", b) if w.lower() not in stop and len(w) > 2}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


async def _fetch_scoreboard(session: aiohttp.ClientSession,
                             sport: str, league: str) -> list[dict]:
    url = f"{ESPN_BASE}/{sport}/{league}/scoreboard"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return []
            data = await r.json(content_type=None)
            return data.get("events", [])
    except Exception as e:
        logger.debug(f"ESPN {sport}/{league}: {e}")
        return []


def _get_win_prob(event: dict, team_name: str) -> tuple[Optional[float], str]:
    """
    Returns (win_probability, home_away) for the matching team in an ESPN event.
    Uses ESPN's own win probability if available, otherwise uses betting odds.
    """
    competitions = event.get("competitions", [{}])
    if not competitions:
        return None, ""
    comp = competitions[0]
    competitors = comp.get("competitors", [])

    best_match = None
    best_score  = 0.0

    for c in competitors:
        t_name = (c.get("team") or {}).get("displayName", "")
        score  = _name_overlap(team_name, t_name)
        if score > best_score:
            best_score = score
            best_match = c

    if best_score < 0.25 or not best_match:
        return None, ""

    home_away = best_match.get("homeAway", "away")

    # Try ESPN win probability from statistics
    for stat in (best_match.get("statistics") or []):
        if stat.get("name") in ("winProbability", "gameProjection"):
            try:
                val = stat.get("displayValue", "")
                prob = float(val.strip("%")) / 100
                if 0 < prob < 1:
                    return prob, home_away
            except Exception:
                pass

    # Fallback: home team wins ~54%, away ~46% historically
    prob = 0.54 if home_away == "home" else 0.46
    return prob, home_away


async def find_sports_opportunities(markets: list[dict],
                                     session: aiohttp.ClientSession) -> list[dict]:
    """
    Given a list of Polymarket markets, find sports bets with edge vs ESPN.

    Returns list of opportunities with same format as Binance fallback.
    """
    if not markets:
        return []

    # Keywords that suggest a sports market
    sports_keywords = [
        "win", "beat", "defeat", "score", "championship", "playoff",
        "nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball",
        "baseball", "hockey", "tennis", "golf", "pga", "wimbledon",
        "premier league", "la liga", "bundesliga", "serie a", "ligue 1",
        "ipl", "cricket", "mls", "spurs", "lakers", "celtics", "warriors",
    ]

    sports_markets = [
        m for m in markets
        if any(kw in m.get("question", "").lower() for kw in sports_keywords)
    ]

    if not sports_markets:
        return []

    logger.info(f"Sports Scout: checking {len(sports_markets)} sports markets")

    # Fetch all league scoreboards concurrently
    tasks = [_fetch_scoreboard(session, sport, league) for sport, league in LEAGUES]
    all_events_lists = await asyncio.gather(*tasks, return_exceptions=True)
    all_events = []
    for evs in all_events_lists:
        if isinstance(evs, list):
            all_events.extend(evs)

    logger.info(f"ESPN returned {len(all_events)} live/upcoming games")

    opps = []
    for market in sports_markets:
        q = market.get("question", "")

        # Try to match market question to an ESPN event
        best_event = None
        best_team_prob = None
        best_score_overall = 0.0
        direction = "YES"

        for event in all_events:
            # Try each competitor as the potential "team we're betting on"
            comp_list = (event.get("competitions") or [{}])[0].get("competitors", [])
            for comp in comp_list:
                team_name = (comp.get("team") or {}).get("displayName", "")
                if not team_name:
                    continue
                match_score = _name_overlap(q, team_name)
                if match_score > best_score_overall:
                    best_score_overall = match_score
                    prob, _ = _get_win_prob(event, team_name)
                    if prob is not None:
                        best_team_prob = prob
                        best_event = event

        if best_score_overall < 0.25 or best_team_prob is None:
            continue

        yes_price = market.get("yes_price", 0.5)
        no_price  = market.get("no_price",  0.5)

        yes_edge = best_team_prob - yes_price
        no_edge  = (1 - best_team_prob) - no_price

        if yes_edge >= MIN_EDGE:
            bet_dir, prob, mp, tid = "YES", best_team_prob, yes_price, market.get("yes_token_id","")
        elif no_edge >= MIN_EDGE:
            bet_dir, prob, mp, tid = "NO", 1-best_team_prob, no_price, market.get("no_token_id","")
        else:
            continue

        event_name = best_event.get("name", "") if best_event else ""
        logger.info(
            f"[SPORTS] {q[:60]} | ESPN prob={best_team_prob:.0%} "
            f"market={yes_price:.2f} edge={yes_edge:+.0%}"
        )

        opps.append({
            "condition_id":   market["condition_id"],
            "question":       q,
            "direction":      bet_dir,
            "token_id":       tid,
            "market_price":   mp,
            "estimated_prob": prob,
            "reason":         f"ESPN win prob {best_team_prob:.0%} vs market {yes_price:.2f} | {event_name[:50]}",
            "neg_risk":       market.get("neg_risk", False),
            "market_url":     market.get("market_url", ""),
            "end_date":       market.get("end_date", ""),
        })

    logger.info(f"Sports Scout found {len(opps)} opportunities")
    return opps
