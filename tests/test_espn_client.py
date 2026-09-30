import json

import requests

from fantasyoptimizer.demo import DEMO_SWID, demo_data
from fantasyoptimizer.espn import EspnClient


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.ok = body, status, status < 400
        self.text = json.dumps(body)

    def json(self):
        return self.body


def test_load_league_requests_and_parses(monkeypatch):
    league_json, free_agents, pro_teams = demo_data()
    rostered = [e["playerPoolEntry"] for t in league_json["teams"] for e in t["roster"]["entries"]]
    calls = []

    def fake_request(self, method, url, params=None, headers=None, **kwargs):
        calls.append((method, url, params, headers))
        views = [v for k, v in (params or []) if k == "view"] if isinstance(params, list) else [params["view"]]
        if views == ["proTeamSchedules_wl"]:
            return Resp({"settings": {"proTeams": pro_teams}})
        if views == ["kona_player_info"]:
            filt = json.loads(headers["X-Fantasy-Filter"])["players"]
            # ESPN rejects a limit without a sort.
            assert "limit" in filt and any(k.startswith("sort") for k in filt)
            return Resp({"players": rostered if "filterIds" in filt else free_agents})
        return Resp(league_json)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    client = EspnClient(424242, 2026, espn_s2="s2cookie", swid=DEMO_SWID.strip("{}"))
    league = client.load_league()
    assert league.my_team_id == 1
    assert client.session.cookies.get("SWID") == DEMO_SWID
    assert len(calls) == 4
    assert calls[0][1].endswith("/games/ffl/seasons/2026/segments/0/leagues/424242")
    assert ("view", "mRoster") in calls[0][2]


def test_errors_are_explained(monkeypatch):
    monkeypatch.setattr(requests.Session, "request", lambda *a, **k: Resp({"messages": ["nope"]}, 401))
    client = EspnClient(1, 2026)
    try:
        client.league()
    except Exception as exc:
        assert "ESPN_S2" in str(exc)
    else:
        raise AssertionError("expected an error")
