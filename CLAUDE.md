# fantasyoptimizer

Daily bot for an ESPN fantasy football team: "Elliott Eats Everyone" (team 4) in the
ScreamStream 2024 League (league 473811221). 10 teams, and the lineup includes a DP
(defensive player) slot.

## Before talking about the team, read the latest run

Every run saves its output on the `reports` branch:

- `latest.md`: the readable report. It has moves with a "Why" line, the roster with
  weekly projections, the best free agents, trade ideas, and league-wide playoff and
  title odds.
- `latest.json`: the full data. It covers every rostered player in the league plus the
  top 10 free agents at each position. Each player has weekly projections (`source: espn` is ESPN's
  number, `bot` is the bot's estimate), rest-of-season points per game, bye week,
  injury, and fantasy team. It also has team records and odds.
- `history/<date>-<time>.md|json`: every past run. Pregame checks that changed the lineup
  are saved here as `<date>-<time>-pregame.*` and don't replace `latest`.

Read them with GitHub's get_file_contents on ref `reports`. If they look stale, the
latest "Daily fantasy moves" Actions run log has the same report.

## Working on the code

- `python -m pytest` runs the tests. `python -m fantasyoptimizer --demo` runs offline
  against a made-up league.
- Tunables live in `optimizer.toml`. The `ESPN_S2` / `ESPN_SWID` cookies are GitHub
  secrets. The `DRY_RUN` repo variable controls whether moves are sent to ESPN.
- ESPN's API is unofficial. See "Things to know" in README.md.
