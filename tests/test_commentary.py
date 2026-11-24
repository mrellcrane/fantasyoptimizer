from types import SimpleNamespace

import pytest

from fantasyoptimizer import commentary, demo
from fantasyoptimizer.config import load_config
from fantasyoptimizer.engine import Optimizer
from fantasyoptimizer.models import parse_league
from fantasyoptimizer.report import render, snapshot
from fantasyoptimizer.state import State


@pytest.fixture(scope="module")
def late_season(tmp_path_factory):
    old = demo.CURRENT_WEEK
    demo.CURRENT_WEEK = 12
    try:
        league = demo.demo_league()
    finally:
        demo.CURRENT_WEEK = old
    cfg = load_config(None, env={})
    cfg.trades.enabled = False
    cfg.sim.n_sims = 1500
    return Optimizer(league, cfg, State()).run()


def test_matchup_scores_are_parsed():
    data, players, pro = demo.demo_data()
    league = parse_league(data, players, pro, team_id=1)
    played = [m for m in league.schedule if m.winner != "UNDECIDED"]
    assert played and all(m.home_points is not None and m.away_points is not None for m in played)
    m = played[0]
    assert m.points(m.home) == m.home_points and m.opponent(m.home) == m.away


def test_story_facts(late_season):
    s = commentary.season_story(late_season)
    league, me = late_season.league, late_season.league.my_team
    assert s["record"] == f"{me.wins:g}-{me.losses:g}"
    assert len(s["game_log"]) == me.wins + me.losses + me.ties
    w, l_ = map(int, s["luck"]["all_play_record"].split("-"))
    assert w + l_ <= len(s["game_log"]) * (len(league.teams) - 1)
    assert s["luck"]["actual_wins"] == me.wins
    sc = s["what_has_to_happen"]
    assert sc["regular_season_games_left"] == len(s["schedule_left"])
    assert all(0 <= r["win_chance"] <= 100 for r in s["schedule_left"])
    assert [r["seed"] for r in s["standings"]] == list(range(1, len(league.teams) + 1))


def test_template_reads_like_prose(late_season):
    text = commentary.template(commentary.season_story(late_season))
    assert "Playoff odds" in text and "—" not in text
    assert text.count("\n\n") >= 3


def test_odds_history(tmp_path, late_season):
    import json
    for name, week, odds in [("2026-11-10-1400", 10, 0.5), ("2026-11-10-2000", 10, 0.4),
                             ("2026-11-11-1400-pregame", 10, 0.9), ("2026-11-17-1400", 11, 0.3)]:
        (tmp_path / f"{name}.json").write_text(json.dumps({
            "generated_at": f"{name[:10]}T14:00:00+00:00", "league": {"week": week},
            "teams": [{"id": 1, "wins": 5, "losses": 5, "playoff_odds": odds, "title_odds": odds / 5}]}))
    (tmp_path / "broken.json").write_text("{")
    rows = commentary.load_odds_history(tmp_path, 1)
    assert [r["playoff_odds"] for r in rows] == [0.4, 0.3]   # last run per day, no pregame
    s = commentary.season_story(late_season, rows)
    assert s["odds_trend"]["season_high"]["playoff_odds"] == 40.0


class FakeClient:
    def __init__(self, stop="end_turn", fail=False):
        self.stop, self.fail, self.calls = stop, fail, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("boom")
        return SimpleNamespace(stop_reason=self.stop, content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text="You won — barely."),
        ])


def test_claude_writes_it_when_available(late_season):
    story = commentary.season_story(late_season)
    client = FakeClient()
    text, by = commentary.write(story, client=client)
    assert by == "claude" and text == "You won, barely."
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5" and story["team"] in call["messages"][0]["content"]


@pytest.mark.parametrize("client", [FakeClient(stop="refusal"), FakeClient(fail=True), None])
def test_falls_back_to_the_template(late_season, client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    story = commentary.season_story(late_season)
    text, by = commentary.write(story, client=client)
    assert by == "template" and text == commentary.template(story)


def test_report_leads_with_the_story(late_season):
    late_season.story = {"team": "x"}
    late_season.commentary = "What a season."
    try:
        out = render(late_season)
        assert out.index("## The story so far") < out.index("## Lineup check")
        assert snapshot(late_season)["commentary"] == "What a season."
    finally:
        late_season.story, late_season.commentary = None, ""


def test_no_dashes():
    assert commentary._no_dashes("A \u2014 b, went 3\u20131, c\u2013d") == "A, b, went 3-1, c, d"
