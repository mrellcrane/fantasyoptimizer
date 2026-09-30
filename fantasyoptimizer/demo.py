"""A made-up league in ESPN's JSON format, for tests and `--demo` runs."""
from __future__ import annotations

import datetime as dt
import random

from .models import League, parse_league

YEAR = 2026
CURRENT_WEEK = 4
FINAL_WEEK = 17
REG_WEEKS = 14
DEMO_SWID = "{DEMO-0000-SWID}"

POSITIONS = {
    # position id: (label, eligible slots, pool size, top season proj, drop per rank, floor)
    1: ("QB", [0, 7, 20, 21], 36, 370, 6.0, 90),
    2: ("RB", [2, 3, 23, 7, 20, 21], 80, 300, 3.3, 25),
    3: ("WR", [4, 3, 5, 23, 7, 20, 21], 100, 310, 2.8, 25),
    4: ("TE", [6, 5, 23, 7, 20, 21], 40, 230, 4.5, 25),
    5: ("K", [17, 20, 21], 32, 150, 1.6, 70),
    16: ("D/ST", [16, 20, 21], 32, 140, 1.8, 60),
}
SLOT_COUNTS = {"0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "16": 1, "17": 1, "20": 7, "21": 1}
MAX_DRAFTED = {1: 2, 2: 6, 3: 6, 4: 2, 5: 1, 16: 1}
TEAM_NAMES = ["Claude's Crushers", "Gridiron Gurus", "Fourth and Long", "The Waiver Wire",
              "Bye Week Blues", "Hail Mary Heroes", "Punt Intended", "Red Zone Rebels",
              "Blitz Brigade", "Sunday Scaries"]


def _stats(rng: random.Random, season_proj: float, bye: int, injury: str) -> list[dict]:
    rate = season_proj / 17
    played = [w for w in range(1, CURRENT_WEEK) if w != bye]
    form = rng.uniform(0.6, 1.45)
    actual = sum(max(0.0, rng.gauss(rate * form, rate * 0.35)) for _ in played)
    stats = [
        {"seasonId": YEAR, "statSourceId": 1, "statSplitTypeId": 0, "scoringPeriodId": 0,
         "appliedTotal": round(season_proj, 2), "appliedAverage": round(rate, 2)},
        {"seasonId": YEAR, "statSourceId": 1, "statSplitTypeId": 2, "scoringPeriodId": 0,
         "appliedTotal": round(rate * (0.7 + 0.3 * form), 2),
         "appliedAverage": round(rate * (0.7 + 0.3 * form), 2)},
    ]
    if played:
        stats.append({"seasonId": YEAR, "statSourceId": 0, "statSplitTypeId": 0, "scoringPeriodId": 0,
                      "appliedTotal": round(actual, 2), "appliedAverage": round(actual / len(played), 2)})
    week = 0.0 if CURRENT_WEEK == bye or injury in ("OUT", "INJURY_RESERVE") else \
        rate * (0.7 + 0.3 * form) * rng.uniform(0.8, 1.2)
    stats.append({"seasonId": YEAR, "statSourceId": 1, "statSplitTypeId": 1,
                  "scoringPeriodId": CURRENT_WEEK, "appliedTotal": round(week, 2)})
    return stats


def demo_data(seed: int = 7) -> tuple[dict, list[dict], list[dict]]:
    """Returns (league json, free agent entries, pro team schedules)."""
    rng = random.Random(seed)
    byes = {pt: 5 + (pt % 10) for pt in range(1, 33)}
    thursday_teams = {3, 17}  # already kicked off this week, so their players are locked
    pro_teams = [{
        "id": pt, "abbrev": f"T{pt}", "byeWeek": byes[pt],
        "proGamesByScoringPeriod": {str(w): [{"id": pt * 100 + w}] for w in range(1, 19) if w != byes[pt]},
    } for pt in range(1, 33)]

    players, next_id = [], 1000
    for pos_id, (label, slots, count, top, drop, floor) in POSITIONS.items():
        for rank in range(count):
            proj = max(floor, top - drop * rank) * rng.uniform(0.85, 1.15)
            pro = rng.randint(1, 32) if pos_id != 16 else rank + 1
            roll = rng.random()
            injury = ("INJURY_RESERVE" if roll < 0.02 else "OUT" if roll < 0.05
                      else "QUESTIONABLE" if roll < 0.12 else "ACTIVE")
            players.append({
                "id": next_id,
                "fullName": f"T{pro} D/ST" if pos_id == 16
                else f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                "defaultPositionId": pos_id, "eligibleSlots": slots, "proTeamId": pro,
                "injuryStatus": injury, "injured": injury != "ACTIVE",
                "ownership": {"percentOwned": 0.0},
                "stats": _stats(rng, proj, byes[pro], injury),
                "_proj": proj,
            })
            next_id += 1

    # Snake draft with a little noise so rosters end up lopsided in different ways.
    teams = list(range(1, len(TEAM_NAMES) + 1))
    rosters: dict[int, list[dict]] = {t: [] for t in teams}
    pool = sorted(players, key=lambda p: -p["_proj"])
    for rnd in range(16):
        order = teams if rnd % 2 == 0 else teams[::-1]
        for t in order:
            counts = {pid: sum(1 for p in rosters[t] if p["defaultPositionId"] == pid) for pid in POSITIONS}
            late = rnd >= 14
            need = [pid for pid in (5, 16) if counts[pid] == 0]
            options = [p for p in pool if counts[p["defaultPositionId"]] < MAX_DRAFTED[p["defaultPositionId"]]
                       and (not late or not need or p["defaultPositionId"] in need)
                       and (late or p["defaultPositionId"] not in (5, 16))]
            pick = options[min(len(options) - 1, int(abs(rng.gauss(0, 1.5))))]
            pool.remove(pick)
            rosters[t].append(pick)

    for p in players:
        p["ownership"]["percentOwned"] = round(min(99.9, p["_proj"] / 4), 1)

    def entry(p: dict, team: int, slot: int) -> dict:
        return {"playerId": p["id"], "lineupSlotId": slot, "playerPoolEntry": {
            "id": p["id"], "onTeamId": team, "status": "ONTEAM",
            "lineupLocked": p["proTeamId"] in thursday_teams, "rosterLocked": False,
            "tradeLocked": False, "player": {k: v for k, v in p.items() if k != "_proj"}}}

    team_json = []
    strength = {}
    for t in teams:
        ranked = sorted(rosters[t], key=lambda p: -p["_proj"])
        need = {0: 1, 2: 2, 4: 2, 6: 1, 16: 1, 17: 1, 23: 1}
        slot_of = {}
        # Start the best projected players, ignoring byes and injuries, like a lazy manager.
        for p in ranked:
            for slot in p["eligibleSlots"]:
                if need.get(slot, 0) > 0:
                    need[slot] -= 1
                    slot_of[p["id"]] = slot
                    break
        ir_used = False
        entries = []
        for p in ranked:
            slot = slot_of.get(p["id"], 20)
            if slot == 20 and p["injuryStatus"] == "INJURY_RESERVE" and not ir_used:
                slot, ir_used = 21, True
            entries.append(entry(p, t, slot))
        strength[t] = sum(p["_proj"] for p in ranked[:9]) / 17
        team_json.append({
            "id": t, "abbrev": f"TM{t}", "name": TEAM_NAMES[t - 1],
            "owners": [DEMO_SWID if t == 1 else f"{{OWNER-{t}}}"],
            "record": {"overall": {"wins": 0, "losses": 0, "ties": 0, "pointsFor": 0.0}},
            "transactionCounter": {"acquisitionBudgetSpent": rng.randint(0, 30)},
            "waiverRank": t, "roster": {"entries": entries},
        })

    schedule = []
    n = len(teams)
    rotation = teams[:]
    for week in range(1, REG_WEEKS + 1):
        for i in range(n // 2):
            home, away = rotation[i], rotation[n - 1 - i]
            m = {"matchupPeriodId": week, "home": {"teamId": home}, "away": {"teamId": away},
                 "winner": "UNDECIDED", "playoffTierType": "NONE"}
            if week < CURRENT_WEEK:
                hs, as_ = rng.gauss(strength[home], 22), rng.gauss(strength[away], 22)
                m["home"]["totalPoints"], m["away"]["totalPoints"] = round(hs, 2), round(as_, 2)
                m["winner"] = "HOME" if hs > as_ else "AWAY"
                for tid, pts, won in ((home, hs, hs > as_), (away, as_, as_ > hs)):
                    rec = team_json[tid - 1]["record"]["overall"]
                    rec["wins" if won else "losses"] += 1
                    rec["pointsFor"] = round(rec["pointsFor"] + pts, 2)
            schedule.append(m)
        rotation = [rotation[0], rotation[-1]] + rotation[1:-1]

    # Someone has sent us an offer: their best WR for our best RB.
    mine = team_json[0]["roster"]["entries"]
    theirs = team_json[2]["roster"]["entries"]
    my_rb = next(e for e in mine if e["playerPoolEntry"]["player"]["defaultPositionId"] == 2)
    their_wr = next(e for e in theirs if e["playerPoolEntry"]["player"]["defaultPositionId"] == 3)
    pending = [{"id": "demo-offer-1", "type": "TRADE_PROPOSAL", "status": "PENDING", "teamId": 3,
                "items": [{"playerId": their_wr["playerId"], "type": "TRADE", "fromTeamId": 3, "toTeamId": 1},
                          {"playerId": my_rb["playerId"], "type": "TRADE", "fromTeamId": 1, "toTeamId": 3}]}]

    deadline = dt.datetime(YEAR, 11, 25, 17, tzinfo=dt.timezone.utc)
    league = {
        "id": 424242, "seasonId": YEAR, "scoringPeriodId": CURRENT_WEEK,
        "status": {"currentMatchupPeriod": CURRENT_WEEK, "finalScoringPeriod": FINAL_WEEK,
                   "latestScoringPeriod": CURRENT_WEEK},
        "settings": {
            "name": "Demo League",
            "rosterSettings": {"lineupSlotCounts": SLOT_COUNTS},
            "scheduleSettings": {"matchupPeriodCount": REG_WEEKS, "playoffTeamCount": 6,
                                 "matchupPeriods": {str(w): [w] for w in range(1, FINAL_WEEK + 1)}},
            "acquisitionSettings": {"isUsingAcquisitionBudget": True, "acquisitionBudget": 100},
            "tradeSettings": {"deadlineDate": int(deadline.timestamp() * 1000)},
        },
        "teams": team_json,
        "schedule": schedule,
        "pendingTransactions": pending,
    }
    free_agents = []
    for i, p in enumerate(pool):
        free_agents.append({
            "id": p["id"], "onTeamId": 0, "status": "WAIVERS" if i % 7 == 0 else "FREEAGENT",
            "lineupLocked": p["proTeamId"] in thursday_teams, "rosterLocked": False, "tradeLocked": False,
            "player": {k: v for k, v in p.items() if k != "_proj"},
        })
    return league, free_agents, pro_teams


def demo_league(seed: int = 7) -> League:
    data, free_agents, pro_teams = demo_data(seed)
    return parse_league(data, free_agents, pro_teams, swid=DEMO_SWID)


FIRST = ["Jalen", "Marcus", "Tyler", "DeShawn", "Cooper", "Bijan", "Amari", "Travis", "Kyle",
         "Jordan", "Malik", "Derrick", "Justin", "Chris", "Tony", "Darius", "Caleb", "Zay"]
LAST = ["Hollins", "Brooks", "Carter", "Reed", "Mason", "Lockett", "Waller", "Fields", "Hunt",
        "Cooks", "Mack", "Pierce", "Shaw", "Bryant", "Coleman", "Dillon", "Harris", "Knox"]
