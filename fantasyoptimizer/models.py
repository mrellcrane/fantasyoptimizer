"""Plain data models, parsed from ESPN's JSON responses."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

# ESPN football lineup slot ids.
SLOT_NAMES = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE",
    7: "OP", 8: "DT", 9: "DE", 10: "LB", 11: "DL", 12: "CB", 13: "S",
    14: "DB", 15: "DP", 16: "D/ST", 17: "K", 18: "P", 19: "HC", 20: "BE",
    21: "IR", 23: "FLEX", 24: "ER",
}
BENCH_SLOT = 20
IR_SLOT = 21
NON_STARTING_SLOTS = {BENCH_SLOT, IR_SLOT}

# ESPN football defaultPositionId values.
POSITION_NAMES = {
    1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 7: "P", 9: "DT", 10: "DE",
    11: "LB", 12: "CB", 13: "S", 14: "HC", 16: "D/ST",
}

FREE_AGENT = "FREEAGENT"
WAIVERS = "WAIVERS"
ON_TEAM = "ONTEAM"


@dataclass
class Player:
    id: int
    name: str
    position: str
    pro_team_id: int
    eligible_slots: frozenset[int]
    injury_status: str = "ACTIVE"
    status: str = FREE_AGENT
    team_id: int = 0
    lineup_slot: int | None = None
    lineup_locked: bool = False
    roster_locked: bool = False
    trade_locked: bool = False
    percent_owned: float = 0.0
    season_projection: float | None = None
    season_actual: float | None = None
    season_average: float | None = None
    ros_rate: float | None = None      # ESPN's rest-of-season points per game
    period_projections: dict[int, float] = field(default_factory=dict)

    @property
    def games_played(self) -> float:
        if self.season_actual and self.season_average:
            return self.season_actual / self.season_average
        return 0.0

    @property
    def available(self) -> bool:
        return self.status in (FREE_AGENT, WAIVERS)

    def __str__(self) -> str:
        return f"{self.name} ({self.position})"


@dataclass
class Team:
    id: int
    name: str
    abbrev: str
    owners: tuple[str, ...]
    wins: float
    losses: float
    ties: float
    points_for: float
    roster: list[int]
    faab_remaining: int | None = None
    waiver_rank: int | None = None


@dataclass(frozen=True)
class Matchup:
    period: int
    home: int
    away: int | None
    winner: str = "UNDECIDED"
    playoff_tier: str = "NONE"


@dataclass
class PendingTrade:
    id: str
    proposer: int
    items: list[dict]
    status: str = "PENDING"

    @property
    def team_ids(self) -> set[int]:
        ids = set()
        for it in self.items:
            ids.update(t for t in (it.get("fromTeamId"), it.get("toTeamId")) if t and t > 0)
        return ids


@dataclass
class League:
    id: int
    year: int
    name: str
    current_scoring_period: int
    current_matchup_period: int
    final_scoring_period: int
    reg_season_periods: int
    matchup_periods: dict[int, list[int]]
    playoff_team_count: int
    slot_counts: dict[int, int]
    teams: dict[int, Team]
    players: dict[int, Player]
    schedule: list[Matchup]
    my_team_id: int
    member_id: str
    uses_faab: bool = False
    faab_budget: int = 0
    trade_deadline: dt.datetime | None = None
    pending_trades: list[PendingTrade] = field(default_factory=list)
    # pro team id -> {scoring period: games that period}
    pro_games: dict[int, dict[int, int]] = field(default_factory=dict)
    pro_team_abbrevs: dict[int, str] = field(default_factory=dict)

    @property
    def my_team(self) -> Team:
        return self.teams[self.my_team_id]

    @property
    def roster_limit(self) -> int:
        """Max players outside IR slots."""
        return sum(c for s, c in self.slot_counts.items() if s != IR_SLOT)

    @property
    def starting_slots(self) -> list[int]:
        slots = []
        for slot, count in sorted(self.slot_counts.items()):
            if slot not in NON_STARTING_SLOTS:
                slots.extend([slot] * count)
        return slots

    def active_count(self, roster: list[int] | tuple[int, ...]) -> int:
        return sum(1 for pid in roster if self.players[pid].lineup_slot != IR_SLOT)

    def games(self, pro_team_id: int, scoring_period: int) -> int:
        if not pro_team_id:
            return 0
        by_period = self.pro_games.get(pro_team_id)
        if by_period is None:
            return 1
        return by_period.get(scoring_period, 0)

    def scoring_periods_of(self, matchup_period: int) -> list[int]:
        return self.matchup_periods.get(matchup_period, [matchup_period])

    @property
    def playoff_periods(self) -> list[int]:
        return sorted(
            mp for mp in self.matchup_periods
            if mp > self.reg_season_periods
            and min(self.scoring_periods_of(mp)) <= self.final_scoring_period
        )

    @property
    def remaining_matchup_periods(self) -> list[int]:
        return sorted(
            mp for mp in self.matchup_periods
            if mp >= self.current_matchup_period
            and min(self.scoring_periods_of(mp)) <= self.final_scoring_period
        )

    @property
    def horizon(self) -> list[int]:
        """Scoring periods still to be played this season, in order."""
        sps = set()
        for mp in self.remaining_matchup_periods:
            sps.update(sp for sp in self.scoring_periods_of(mp)
                       if self.current_scoring_period <= sp <= self.final_scoring_period)
        return sorted(sps)

    def team_name(self, team_id: int) -> str:
        t = self.teams.get(team_id)
        return t.name if t else f"Team {team_id}"

    def find_player(self, name_or_id: str | int) -> Player | None:
        if isinstance(name_or_id, int) or str(name_or_id).isdigit():
            return self.players.get(int(name_or_id))
        needle = str(name_or_id).strip().lower()
        for p in self.players.values():
            if p.name.lower() == needle:
                return p
        return None


# ---------------------------------------------------------------- parsing

def _parse_stats(player: Player, stats: list[dict], year: int) -> None:
    for s in stats or []:
        if s.get("seasonId") not in (None, year):
            continue
        source, split = s.get("statSourceId"), s.get("statSplitTypeId")
        total = s.get("appliedTotal")
        if total is None:
            continue
        if source == 1 and split == 0:
            player.season_projection = float(total)
        elif source == 0 and split == 0:
            player.season_actual = float(total)
            avg = s.get("appliedAverage")
            player.season_average = float(avg) if avg is not None else None
        elif source == 1 and split == 1 and s.get("scoringPeriodId"):
            player.period_projections[int(s["scoringPeriodId"])] = float(total)
        elif source == 1 and split == 2:
            avg = s.get("appliedAverage")
            player.ros_rate = float(avg if avg is not None else total)


def parse_player(entry: dict, year: int) -> Player:
    """Parse a playerPoolEntry-style dict ({player: {...}, status, onTeamId...})."""
    raw = entry.get("player") or entry
    pos_id = raw.get("defaultPositionId", 0)
    p = Player(
        id=int(raw.get("id", entry.get("id"))),
        name=raw.get("fullName") or f"Player {raw.get('id')}",
        position=POSITION_NAMES.get(pos_id, str(pos_id)),
        pro_team_id=int(raw.get("proTeamId") or 0),
        eligible_slots=frozenset(int(s) for s in raw.get("eligibleSlots", [])),
        injury_status=(raw.get("injuryStatus") or entry.get("injuryStatus") or "ACTIVE").upper(),
        status=entry.get("status") or FREE_AGENT,
        team_id=int(entry.get("onTeamId") or 0),
        lineup_locked=bool(entry.get("lineupLocked", False)),
        roster_locked=bool(entry.get("rosterLocked", False)),
        trade_locked=bool(entry.get("tradeLocked", False)),
        percent_owned=float((raw.get("ownership") or {}).get("percentOwned") or 0.0),
    )
    if p.injury_status == "NORMAL":
        p.injury_status = "ACTIVE"
    _parse_stats(p, raw.get("stats", []), year)
    return p


def _merge_player(base: Player, extra: Player) -> None:
    """Fill in stats from a second fetch of the same player."""
    if base.season_projection is None:
        base.season_projection = extra.season_projection
    if base.ros_rate is None:
        base.ros_rate = extra.ros_rate
    if base.season_actual is None:
        base.season_actual, base.season_average = extra.season_actual, extra.season_average
    for sp, pts in extra.period_projections.items():
        base.period_projections.setdefault(sp, pts)
    if base.percent_owned == 0.0:
        base.percent_owned = extra.percent_owned
    base.lineup_locked |= extra.lineup_locked
    base.roster_locked |= extra.roster_locked
    base.trade_locked |= extra.trade_locked


def normalize_swid(swid: str | None) -> str:
    if not swid:
        return ""
    swid = swid.strip().upper()
    if not swid.startswith("{"):
        swid = "{" + swid + "}"
    return swid


def parse_league(
    data: dict,
    player_entries: list[dict],
    pro_teams: list[dict] | None,
    *,
    swid: str | None = None,
    team_id: int | None = None,
) -> League:
    """Build a League from the league JSON, kona_player_info players, and pro schedules."""
    year = int(data.get("seasonId"))
    settings = data.get("settings", {})
    status = data.get("status", {})
    sched = settings.get("scheduleSettings", {})
    acq = settings.get("acquisitionSettings", {})
    trade_settings = settings.get("tradeSettings", {})
    swid = normalize_swid(swid)

    players: dict[int, Player] = {}
    teams: dict[int, Team] = {}
    budget = int(acq.get("acquisitionBudget") or 0)
    uses_faab = bool(acq.get("isUsingAcquisitionBudget"))

    for t in data.get("teams", []):
        roster = []
        for e in (t.get("roster") or {}).get("entries", []):
            pool = dict(e.get("playerPoolEntry") or {})
            pool.setdefault("status", ON_TEAM)
            pool.setdefault("onTeamId", t["id"])
            p = parse_player(pool, year)
            p.status, p.team_id = ON_TEAM, t["id"]
            p.lineup_slot = e.get("lineupSlotId")
            if e.get("injuryStatus") and p.injury_status == "ACTIVE":
                p.injury_status = e["injuryStatus"].upper().replace("NORMAL", "ACTIVE")
            players[p.id] = p
            roster.append(p.id)
        rec = (t.get("record") or {}).get("overall", {})
        name = t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}".strip()
        spent = (t.get("transactionCounter") or {}).get("acquisitionBudgetSpent") or 0
        teams[t["id"]] = Team(
            id=t["id"],
            name=name or f"Team {t['id']}",
            abbrev=t.get("abbrev", ""),
            owners=tuple(normalize_swid(o) for o in t.get("owners", [])),
            wins=float(rec.get("wins", 0)),
            losses=float(rec.get("losses", 0)),
            ties=float(rec.get("ties", 0)),
            points_for=float(rec.get("pointsFor", 0.0)),
            roster=roster,
            faab_remaining=budget - int(spent) if uses_faab else None,
            waiver_rank=t.get("waiverRank"),
        )

    for entry in player_entries:
        p = parse_player(entry, year)
        if p.id in players:
            _merge_player(players[p.id], p)
        elif p.status != ON_TEAM:
            players[p.id] = p

    my_team_id = team_id
    if my_team_id is None and swid:
        for t in teams.values():
            if swid in t.owners:
                my_team_id = t.id
                break
    if my_team_id is None or my_team_id not in teams:
        raise ValueError(
            "Could not determine your team. Set ESPN_TEAM_ID, or make sure ESPN_SWID "
            "belongs to a team owner in this league."
        )

    schedule = []
    for m in data.get("schedule", []):
        home = (m.get("home") or {}).get("teamId")
        if home is None:
            continue
        away = (m.get("away") or {}).get("teamId")
        schedule.append(Matchup(
            period=int(m["matchupPeriodId"]), home=int(home),
            away=int(away) if away is not None else None,
            winner=m.get("winner", "UNDECIDED"),
            playoff_tier=m.get("playoffTierType", "NONE"),
        ))

    matchup_periods = {
        int(k): [int(x) for x in v] for k, v in (sched.get("matchupPeriods") or {}).items()
    }
    reg = int(sched.get("matchupPeriodCount") or 14)
    final_sp = int(status.get("finalScoringPeriod") or max(
        [sp for v in matchup_periods.values() for sp in v] or [17]))
    if not matchup_periods:
        matchup_periods = {i: [i] for i in range(1, final_sp + 1)}

    current_sp = int(data.get("scoringPeriodId") or status.get("latestScoringPeriod") or 1)
    current_mp = int(status.get("currentMatchupPeriod") or next(
        (mp for mp, sps in sorted(matchup_periods.items()) if current_sp in sps), 1))

    deadline = trade_settings.get("deadlineDate")
    trade_deadline = (dt.datetime.fromtimestamp(deadline / 1000, tz=dt.timezone.utc)
                      if deadline else None)

    pro_games: dict[int, dict[int, int]] = {}
    abbrevs: dict[int, str] = {}
    for pt in pro_teams or []:
        pid = int(pt["id"])
        abbrevs[pid] = pt.get("abbrev", "")
        by_sp = pt.get("proGamesByScoringPeriod")
        if by_sp:
            pro_games[pid] = {int(k): len(v) for k, v in by_sp.items()}
        elif pt.get("byeWeek") is not None:
            pro_games[pid] = {sp: (0 if sp == pt["byeWeek"] else 1)
                              for sp in range(1, final_sp + 1)}

    return League(
        id=int(data.get("id")),
        year=year,
        name=settings.get("name", f"League {data.get('id')}"),
        current_scoring_period=current_sp,
        current_matchup_period=current_mp,
        final_scoring_period=final_sp,
        reg_season_periods=reg,
        matchup_periods=matchup_periods,
        playoff_team_count=int(sched.get("playoffTeamCount") or 4),
        slot_counts={int(k): int(v) for k, v in
                     (settings.get("rosterSettings") or {}).get("lineupSlotCounts", {}).items()
                     if v},
        teams=teams,
        players=players,
        schedule=schedule,
        my_team_id=my_team_id,
        member_id=swid,
        uses_faab=uses_faab,
        faab_budget=budget,
        trade_deadline=trade_deadline,
        pending_trades=parse_pending_trades(data),
        pro_games=pro_games,
        pro_team_abbrevs=abbrevs,
    )


def parse_pending_trades(data: dict) -> list[PendingTrade]:
    """Pending trade proposals, from the mPendingTransactions / mTransactions2 views."""
    raw = list(data.get("pendingTransactions") or []) + list(data.get("transactions") or [])
    seen, out = set(), []
    for tx in raw:
        if tx.get("type") != "TRADE_PROPOSAL" or tx.get("status") not in ("PENDING", "PROPOSED"):
            continue
        tx_id = str(tx.get("id"))
        if tx_id in seen:
            continue
        seen.add(tx_id)
        out.append(PendingTrade(
            id=tx_id, proposer=int(tx.get("teamId") or 0),
            items=[i for i in tx.get("items", []) if i.get("type") in ("TRADE", None)],
            status=tx.get("status", "PENDING"),
        ))
    return out
