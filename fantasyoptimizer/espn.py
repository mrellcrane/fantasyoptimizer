"""Thin client for ESPN's unofficial fantasy API (reads + transaction writes).

ESPN has no public API. The endpoints and payloads here mirror what the
fantasy.espn.com web app sends. The parser is strict (unknown or null keys
return 400), so payloads only carry keys the web app sends.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time

import requests

from .models import NON_STARTING_SLOTS, League, normalize_swid, parse_league

log = logging.getLogger(__name__)

READ_BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/{game}/seasons/{year}"
WRITE_BASE = ("https://lm-api-writes.fantasy.espn.com/apis/v3/games/{game}/seasons/{year}"
              "/segments/0/leagues/{league_id}/transactions/")
LEAGUE_VIEWS = ["mSettings", "mTeam", "mRoster", "mMatchup", "mStatus", "mPendingTransactions"]


class EspnError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: object = None):
        super().__init__(message)
        self.status = status
        self.body = body


class EspnClient:
    def __init__(self, league_id: int, year: int, espn_s2: str | None = None,
                 swid: str | None = None, game: str = "ffl", timeout: float = 30,
                 session: requests.Session | None = None):
        self.league_id = league_id
        self.year = year
        self.game = game
        self.swid = normalize_swid(swid)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "x-fantasy-source": "kona",
            "x-fantasy-platform": "kona-PROD",
        })
        if espn_s2:
            self.session.cookies.set("espn_s2", espn_s2, domain=".espn.com")
        if self.swid:
            self.session.cookies.set("SWID", self.swid, domain=".espn.com")
        self.has_auth = bool(espn_s2)

    # ------------------------------------------------------------ http

    def _request(self, method: str, url: str, **kwargs) -> dict:
        for attempt in range(4):
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            break
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        if resp.status_code in (401, 403):
            raise EspnError(
                f"ESPN returned {resp.status_code}. For a private league, set ESPN_S2 and "
                "ESPN_SWID (they expire occasionally; grab fresh ones from your browser).",
                resp.status_code, body)
        if not resp.ok:
            raise EspnError(f"ESPN {method} {url} failed with {resp.status_code}: {body}",
                            resp.status_code, body)
        return body

    @property
    def _league_url(self) -> str:
        return READ_BASE.format(game=self.game, year=self.year) + f"/segments/0/leagues/{self.league_id}"

    # ------------------------------------------------------------ reads

    def league(self, views: list[str] | None = None, scoring_period: int | None = None) -> dict:
        params = [("view", v) for v in (views or LEAGUE_VIEWS)]
        if scoring_period:
            params.append(("scoringPeriodId", scoring_period))
        return self._request("GET", self._league_url, params=params)

    def players(self, player_filter: dict, scoring_period: int | None = None) -> list[dict]:
        params = [("view", "kona_player_info")]
        if scoring_period:
            params.append(("scoringPeriodId", scoring_period))
        headers = {"X-Fantasy-Filter": json.dumps(player_filter)}
        return self._request("GET", self._league_url, params=params, headers=headers).get("players", [])

    def pro_teams(self) -> list[dict]:
        url = READ_BASE.format(game=self.game, year=self.year)
        data = self._request("GET", url, params={"view": "proTeamSchedules_wl"})
        return (data.get("settings") or {}).get("proTeams", [])

    def load_league(self, team_id: int | None = None, free_agent_limit: int = 250) -> League:
        """Everything the optimizer needs, in four requests."""
        data = self.league()
        sp = data.get("scoringPeriodId")
        slots = sorted({int(s) for s, n in ((data.get("settings") or {}).get("rosterSettings") or {})
                       .get("lineupSlotCounts", {}).items() if n and int(s) not in NON_STARTING_SLOTS})
        rostered = [e["playerId"] for t in data.get("teams", [])
                    for e in (t.get("roster") or {}).get("entries", [])]
        entries = []
        if rostered:
            entries += self.players({"players": {
                "filterIds": {"value": rostered},
                "limit": len(rostered),
                "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
            }}, sp)
        entries += self.players({"players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
            "filterSlotIds": {"value": slots},
            "limit": free_agent_limit,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        }}, sp)
        final = (data.get("status") or {}).get("finalScoringPeriod") or 0
        if sp and sp < final:
            # ESPN's projections for next week, when it has published them.
            ids = sorted({e.get("id") or (e.get("player") or {}).get("id") for e in entries} - {None})
            try:
                entries += self.players({"players": {
                    "filterIds": {"value": ids},
                    "limit": len(ids),
                    "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
                }}, sp + 1)
            except EspnError as exc:
                log.warning("Could not load next week's projections (%s)", exc)
        try:
            pro = self.pro_teams()
        except EspnError as exc:
            log.warning("Could not load pro schedules (%s); assuming no byes", exc)
            pro = []
        return parse_league(data, entries, pro, swid=self.swid, team_id=team_id)

    # ------------------------------------------------------------ writes

    def submit(self, payload: dict) -> dict:
        if not self.has_auth:
            raise EspnError("Writing to ESPN needs ESPN_S2 (and ESPN_SWID).")
        url = WRITE_BASE.format(game=self.game, year=self.year, league_id=self.league_id)
        return self._request("POST", url, json=payload)


# ---------------------------------------------------------------- payloads

def _envelope(league: League, kind: str, items: list[dict]) -> dict:
    payload = {
        "isLeagueManager": False,
        "teamId": league.my_team_id,
        "type": kind,
        "scoringPeriodId": league.current_scoring_period,
        "executionType": "EXECUTE",
        "items": items,
    }
    if league.member_id:
        payload["memberId"] = league.member_id
    return payload


def add_drop_payload(league: League, add_id: int, drop_id: int | None = None,
                     waiver: bool = False, bid: int | None = None) -> dict:
    me = league.my_team_id
    items = [{"playerId": add_id, "type": "ADD", "toTeamId": me}]
    if drop_id is not None:
        items.append({"playerId": drop_id, "type": "DROP", "fromTeamId": me})
    payload = _envelope(league, "WAIVER" if waiver else "FREEAGENT", items)
    if waiver and league.uses_faab:
        payload["bidAmount"] = int(bid or 0)
    return payload


def lineup_payload(league: League, moves: list[tuple[int, int, int]]) -> dict:
    """moves: (player_id, from_slot, to_slot). Only changed slots may be sent."""
    items = [{"playerId": pid, "type": "LINEUP", "fromLineupSlotId": src, "toLineupSlotId": dst}
             for pid, src, dst in moves if src != dst]
    return _envelope(league, "ROSTER", items)


def trade_payload(league: League, partner_id: int, give: list[int], get: list[int],
                  my_drops: list[int] = (), comment: str = "", expiration_hours: int = 48,
                  now: dt.datetime | None = None) -> dict:
    me = league.my_team_id
    items = [{"playerId": pid, "type": "TRADE", "fromTeamId": me, "toTeamId": partner_id} for pid in give]
    items += [{"playerId": pid, "type": "TRADE", "fromTeamId": partner_id, "toTeamId": me} for pid in get]
    items += [{"playerId": pid, "type": "DROP", "fromTeamId": me} for pid in my_drops]
    payload = _envelope(league, "TRADE_PROPOSAL", items)
    now = now or dt.datetime.now(dt.timezone.utc)
    expires = now + dt.timedelta(hours=expiration_hours)
    payload["expirationDate"] = expires.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    payload["comment"] = comment or ""
    return payload

