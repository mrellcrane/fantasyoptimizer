import datetime as dt
import json

import pytest

from fantasyoptimizer import espn, report
from fantasyoptimizer.config import Config, load_config
from fantasyoptimizer.demo import DEMO_SWID, demo_data, demo_league
from fantasyoptimizer.engine import Optimizer
from fantasyoptimizer.models import BENCH_SLOT, IR_SLOT
from fantasyoptimizer.state import State

NOW = dt.datetime(2026, 9, 30, 15, tzinfo=dt.timezone.utc)


class FakeClient:
    def __init__(self, fail_on: str | None = None):
        self.sent = []
        self.fail_on = fail_on

    def submit(self, payload):
        self.sent.append(payload)
        if payload["type"] == self.fail_on:
            raise espn.EspnError("409 TRAN_LINEUP_LOCKED", 409)
        return {"status": "EXECUTED" if payload["type"] in ("FREEAGENT", "ROSTER") else "PENDING"}


def config(dry_run=True):
    cfg = Config(dry_run=dry_run)
    cfg.sim.n_sims = 1500
    cfg.trades.pool_size = 6
    return cfg


def no_none(obj):
    if isinstance(obj, dict):
        return all(v is not None and no_none(v) for v in obj.values())
    if isinstance(obj, list):
        return all(no_none(v) for v in obj)
    return True


def test_demo_parses():
    league = demo_league()
    assert league.my_team.name == "Claude's Crushers"
    assert league.roster_limit == 16
    assert league.horizon == list(range(4, 18))
    assert league.playoff_periods == [15, 16, 17]
    assert len(league.pending_trades) == 1
    p = league.players[league.my_team.roster[0]]
    assert p.ros_rate and p.season_projection and 4 in p.period_projections
    pro = p.pro_team_id
    bye = next(w for w in range(1, 19) if league.games(pro, w) == 0)
    assert 5 <= bye <= 14


def test_dry_run_sends_nothing():
    client = FakeClient()
    result = Optimizer(demo_league(), config(), State(), client, NOW).run()
    assert client.sent == []
    assert result.actions and all(not a.executed for a in result.actions)
    text = report.render(result)
    assert "Dry run" in text and "League outlook" in text
    assert "## Your roster" in text and "## Best available players" in text
    assert result.long_shots and "## Long shots" in text
    assert not {i.key for i in result.long_shots} & {i.key for i in result.trade_ideas}
    assert all(i.my_title_gain > 0 for i in result.long_shots)
    fa_rows = [l for l in text.split("## Best available players")[1].split("## League")[0].splitlines()
               if l.startswith("| ") and not l.startswith("| Pos")]
    positions = [row.split("|")[1].strip() for row in fa_rows]
    assert positions[:3] == ["QB"] * 3 and positions.count("RB") == 3 and "K" in positions
    assert all("of 14" in row for row in fa_rows)
    assert "| Wk 4 | Wk 5 | Wk 6 |" in text and "BYE" in text
    snap = report.snapshot(result)
    json.dumps(snap)  # serializable
    mine = [p for p in snap["players"] if p["fantasy_team"] == "Claude's Crushers"]
    assert len(mine) == len(result.league.my_team.roster)
    assert mine[0]["projections"]["4"]["source"] == "espn"
    assert mine[0]["projections"]["5"]["source"] == "bot"
    for action in result.actions:
        if action.kind in ("add", "waiver", "trade"):
            assert "would start" in action.why and "pts/gm" in action.why
            assert f"Why: {action.why}" in text
    assert "\u2014" not in text  # no em-dashes in anything we show


def test_live_run_sends_valid_payloads_and_records_state(tmp_path):
    client = FakeClient()
    state = State(path=tmp_path / "state.json")
    league = demo_league()
    result = Optimizer(league, config(dry_run=False), state, client, NOW).run()
    kinds = [p["type"] for p in client.sent]
    assert kinds[0] in ("FREEAGENT", "WAIVER")
    assert "ROSTER" in kinds and "TRADE_PROPOSAL" in kinds
    for payload in client.sent:
        assert no_none(payload)
        assert payload["teamId"] == league.my_team_id
        assert payload["scoringPeriodId"] == league.current_scoring_period
        assert payload["executionType"] == "EXECUTE"
    lineup = next(p for p in client.sent if p["type"] == "ROSTER")
    for item in lineup["items"]:
        player = league.players[item["playerId"]]
        assert not player.lineup_locked
        assert item["fromLineupSlotId"] != item["toLineupSlotId"]
        assert IR_SLOT not in (item["fromLineupSlotId"], item["toLineupSlotId"])
    trade = next(p for p in client.sent if p["type"] == "TRADE_PROPOSAL")
    assert trade["expirationDate"].endswith("Z")
    assert all(a.ok for a in result.actions)
    state.save()
    saved = State.load(tmp_path / "state.json")
    assert saved.proposals and saved.adds


def test_lineup_benches_players_on_bye_or_out():
    league = demo_league()
    cfg = config(dry_run=False)
    cfg.waivers.enabled = cfg.trades.enabled = False
    client = FakeClient()
    Optimizer(league, cfg, State(), client, NOW).run()
    moves = client.sent[0]["items"] if client.sent else []
    after = {p: league.players[p].lineup_slot for p in league.my_team.roster}
    for m in moves:
        after[m["playerId"]] = m["toLineupSlotId"]
    for pid, slot in after.items():
        p = league.players[pid]
        if slot not in (BENCH_SLOT, IR_SLOT) and not p.lineup_locked:
            assert p.period_projections.get(4, 0) > 0, f"{p} starts with no projection"


def test_failed_write_is_reported_not_raised():
    client = FakeClient(fail_on="ROSTER")
    cfg = config(dry_run=False)
    cfg.waivers.enabled = cfg.trades.enabled = False
    result = Optimizer(demo_league(), cfg, State(), client, NOW).run()
    assert result.actions[0].executed and result.actions[0].ok is False
    assert "FAILED" in report.render(result)


def test_trade_deadline_skips_trades():
    league = demo_league()
    league.trade_deadline = NOW - dt.timedelta(days=1)
    client = FakeClient()
    result = Optimizer(league, config(dry_run=False), State(), client, NOW).run()
    assert all(p["type"] != "TRADE_PROPOSAL" for p in client.sent)
    assert any("deadline" in n for n in result.notes)


def test_incoming_offer_is_evaluated():
    result = Optimizer(demo_league(), config(), State(), None, NOW).run()
    assert len(result.incoming) == 1
    offer = result.incoming[0]
    assert offer.partner == 3 and offer.give and offer.get


def test_payload_shapes():
    league = demo_league()
    fa = espn.add_drop_payload(league, 1, 2)
    assert fa["type"] == "FREEAGENT" and "bidAmount" not in fa
    assert fa["items"] == [{"playerId": 1, "type": "ADD", "toTeamId": 1},
                           {"playerId": 2, "type": "DROP", "fromTeamId": 1}]
    claim = espn.add_drop_payload(league, 1, None, waiver=True, bid=7)
    assert claim["type"] == "WAIVER" and claim["bidAmount"] == 7
    league.uses_faab = False
    assert "bidAmount" not in espn.add_drop_payload(league, 1, None, waiver=True, bid=7)
    trade = espn.trade_payload(league, 3, [1], [2, 3], [4], now=NOW)
    assert trade["expirationDate"] == "2026-10-02T15:00:00.000Z"
    assert [i["type"] for i in trade["items"]] == ["TRADE", "TRADE", "TRADE", "DROP"]
    assert espn.lineup_payload(league, [(1, 20, 20), (2, 20, 4)])["items"] == [
        {"playerId": 2, "type": "LINEUP", "fromLineupSlotId": 20, "toLineupSlotId": 4}]


def test_config_file_and_env(tmp_path):
    path = tmp_path / "optimizer.toml"
    path.write_text('[general]\ndry_run = false\n[trades]\nmin_gain_points = 3.5\n'
                    'untouchable = ["Some Guy"]\n[injury]\nquestionable = [0.5]\n'
                    '[questionable_performance]\nWR = 0.8\n')
    cfg = load_config(path, env={"ESPN_LEAGUE_ID": "99", "DRY_RUN": "true", "ESPN_YEAR": "2026"})
    assert cfg.league_id == 99 and cfg.year == 2026 and cfg.dry_run is True
    assert cfg.trades.min_gain_points == 3.5 and cfg.trades.untouchable == ["Some Guy"]
    assert cfg.injury["QUESTIONABLE"] == [0.5] and "OUT" in cfg.injury
    assert cfg.questionable_performance == {"WR": 0.8}


def test_team_detected_from_swid_or_error():
    from fantasyoptimizer.models import parse_league
    data, fas, pro = demo_data()
    assert parse_league(data, fas, pro, swid="demo-0000-swid").my_team_id == 1
    assert parse_league(data, fas, pro, team_id=4).my_team_id == 4
    with pytest.raises(ValueError):
        parse_league(data, fas, pro, swid="{NOBODY}")


def test_waiver_claim_does_not_change_todays_lineup():
    league = demo_league()
    for p in league.players.values():
        if p.available:
            p.status = "WAIVERS"
    cfg = config(dry_run=False)
    cfg.trades.enabled = False
    client = FakeClient()
    Optimizer(league, cfg, State(), client, NOW).run()
    claim = next(p for p in client.sent if p["type"] == "WAIVER")
    claimed = next(i["playerId"] for i in claim["items"] if i["type"] == "ADD")
    for payload in (p for p in client.sent if p["type"] == "ROSTER"):
        assert claimed not in {i["playerId"] for i in payload["items"]}


def test_lineup_check_swaps_and_next_week_holes():
    league = demo_league()
    result = Optimizer(league, config(), State(), None, NOW).run()
    check = result.lineup_check
    assert check.week == 4 and check.next_week == 5
    assert check.swaps and check.gain > 0
    for start, sit in check.swaps:  # same kind of spot, and the starter projects higher
        assert league.players[start].position == league.players[sit].position
        v = result.valuer
        assert v.points[v.row[start], 0] >= v.points[v.row[sit], 0]
    tes = [h for h in check.holes if league.players[h.player].position == "TE"]
    assert tes and tes[0].reason == "on bye" and tes[0].fill is not None
    text = report.render(result)
    assert text.index("## Lineup check") < text.index("## Moves")


def _names(league, team_id, pos, n):
    return [league.players[pid].name for pid in league.teams[team_id].roster
            if league.players[pid].position == pos][:n]


def test_score_a_trade_typed_in_by_name():
    league = demo_league()
    give = _names(league, 1, "RB", 1) + _names(league, 1, "QB", 1)
    get = _names(league, 3, "WR", 1)
    text = f"give {give[0]}, {give[1]}; get {get[0]}"
    result = Optimizer(league, config(), State(), None, NOW, score_trade=text).run()
    t = result.asked_trade
    assert t and t.partner == 3 and len(t.give) == 2 and len(t.get) == 1
    assert t.my_title_gain is not None and "For them:" in result.asked_trade_why
    rendered = report.render(result)
    assert "## The trade you asked about" in rendered
    assert rendered.index("The trade you asked about") < rendered.index("## Moves")
    # "A for B" works too
    alt = Optimizer(demo_league(), config(), State(), None, NOW,
                    score_trade=f"{give[0]} for {get[0]}").run()
    assert alt.asked_trade and alt.asked_trade.give == t.give[:1]


def test_bad_trade_text_is_explained_not_crashed():
    league = demo_league()
    mine = _names(league, 1, "RB", 1)[0]
    theirs = _names(league, 3, "WR", 1)[0] + ", " + _names(league, 4, "WR", 1)[0]
    for text, message in [("give Nobody Atall; get Someone Else", "Couldn't find"),
                          (f"give {mine}; get {theirs}", "same team"),
                          ("just vibes", "Write the trade")]:
        result = Optimizer(demo_league(), config(), State(), None, NOW, score_trade=text).run()
        assert result.asked_trade is None and message in result.asked_trade_error
        assert message in report.render(result)


def test_kickoff_times_parsed_from_pro_schedule():
    from fantasyoptimizer.models import parse_league
    data, fas, pro = demo_data()
    pro[0]["proGamesByScoringPeriod"]["4"] = [{"id": 1, "date": 1791133200000}]
    league = parse_league(data, fas, pro, swid=DEMO_SWID)
    kick = league.kickoffs[pro[0]["id"]][4]
    assert kick.tzinfo is not None and kick.year == 2026
    league.loaded_at = kick - dt.timedelta(minutes=60)
    assert league.inactives_out(pro[0]["id"], 4)
    league.loaded_at = kick - dt.timedelta(hours=5)
    assert not league.inactives_out(pro[0]["id"], 4)


def test_zero_proposals_means_ideas_but_no_offers_sent():
    cfg = config(dry_run=False)
    cfg.trades.max_proposals_per_run = 0
    client = FakeClient()
    result = Optimizer(demo_league(), cfg, State(), client, NOW).run()
    assert result.trade_ideas
    assert all(p["type"] != "TRADE_PROPOSAL" for p in client.sent)
    assert any(p["type"] == "ROSTER" for p in client.sent)
