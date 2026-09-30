# fantasyoptimizer

A bot that manages an ESPN fantasy football team once a day, with one goal: raise your odds of winning the championship.

Every run, it:

1. **Picks up free agents.** It finds the add/drop (or waiver claim, with a FAAB bid) that most improves your team for the rest of the season.
2. **Sets your lineup.** It benches players who are on bye, OUT, or projected low, and starts the best available players. Players whose games have started stay put.
3. **Proposes trades.** It looks for deals that make you better *and* fill a real need on the other manager's team, since a lopsided offer just gets rejected. Players are valued by how much better they are than the best free agent at their position. Each idea comes with a one-line pitch to send them.
4. **Grades trade offers sent to you.** It tells you whether to accept. It never accepts on its own.
5. **Writes a report** covering your playoff and title odds, your roster's projections for the next few weeks, what it did and why, the best pickups and trade ideas it found, and a table of the whole league's odds.

## How it decides

- **Player projections.** Each player gets an expected points-per-game. It blends ESPN's projection for this week, ESPN's rest-of-season projection, the preseason projection, and actual production so far (trusted more as games pile up). Byes come from the NFL schedule, and injuries lower availability for the next few weeks (see `[injury]` in `optimizer.toml`).
- **Roster value.** For every remaining week, it solves for your best possible starting lineup given your slots (FLEX, superflex, and so on). A roster's value is the sum of those lineups, with fantasy playoff weeks weighted 1.5x and a little credit for bench depth. That means a backup RB is worth exactly as much as the weeks he'd actually start.
- **Title odds.** It simulates the rest of your league's season 4,000 times, using the real schedule, current records, and each team's projected lineups, then plays out the playoff bracket. Every candidate move is re-simulated with the same random draws, so the difference in title odds reflects the move and not luck. This also catches trades that help you a little but help a rival more.
- **Trade acceptance.** Humans judge trades by value and by whether a deal fills a need. Value is measured as points above what's free on waivers, since a linebacker scoring 19 a game isn't worth much when one scoring 18.6 is free. Need is how much the deal improves *their* best lineup. It models the chance of acceptance from both, and ranks offers by *your title odds gained × chance they accept*.

## Setup (about 10 minutes)

### 1. Get your ESPN credentials

- **League ID:** in your league's URL, `leagueId=XXXXXXX`.
- **Cookies** (needed for private leagues and for making moves): log into fantasy.espn.com on a desktop browser. Open DevTools (F12), go to **Application → Cookies → https://fantasy.espn.com**, and copy the values of `espn_s2` and `SWID`.

These cookies are effectively your ESPN login, so only put them in GitHub secrets, never in a file.

### 2. Add them to this repo

Your league and team ids are already in `optimizer.toml` (`[general]`). The cookies go in as secrets: **Settings → Secrets and variables → Actions → New repository secret**.

| Kind | Name | Value |
|---|---|---|
| Secret | `ESPN_S2` | the `espn_s2` cookie |
| Secret | `ESPN_SWID` | the `SWID` cookie, braces included |
| Secret (optional) | `NOTIFY_URL` | an [ntfy.sh](https://ntfy.sh) topic URL, or a Discord/Slack webhook, for a phone ping after each run |
| Variable | `DRY_RUN` | leave unset (dry run) until you trust it, then set to `false` |

### 3. Try it

Go to **Actions → Daily fantasy moves → Run workflow**, and pick `dry-run`. Open the run and read the summary. It shows exactly what it *would* have done.

Run a few dry runs. When you like what you see, set the `DRY_RUN` variable to `false`. After that it runs daily at 10:17am ET, plus a lineup-only run on Sundays at 12:07pm ET after inactives are announced.

You can also run it locally:

```bash
pip install -r requirements.txt
python -m fantasyoptimizer --demo                  # made-up league, no credentials
ESPN_LEAGUE_ID=... ESPN_S2=... ESPN_SWID=... python -m fantasyoptimizer   # dry run
python -m fantasyoptimizer --live                  # actually send moves
```

## Reports

Each run saves its report on the `reports` branch:

- `latest.md` is the report you'd read. It has your roster with this week's and the next two weeks' projections, the best available players, moves with the reasoning behind them, trade ideas, and the league's odds.
- `latest.json` has the same data for every rostered player in the league plus the top 10 free agents at each position. It's handy for digging into trades or asking Claude about it.
- `history/` keeps every past run.

Weekly numbers come from ESPN when ESPN has published them. A `~` marks the bot's own estimate.

## Tuning

Everything lives in `optimizer.toml`, with comments. The ones you'll most likely touch:

- `trades.untouchable`: players it must never offer.
- `trades.do_not_trade_with`: managers to leave alone.
- `trades.max_proposals_per_run` / `team_cooldown_days`: how pushy it is. The default is one offer a day, max one per manager per week, and never the same offer twice in 30 days.
- `trades.message` / `trades.pitch_as_message`: a note attached to each offer, or the one-line pitch the report writes for each trade ("Burrow would start every week for you over...").
- `trades.min_fairness` / `min_accept_chance`: how lopsided offers may be. There's a commented "bolder" setting in the file.
- `trades.long_shots`: the report's "Long shots" list of trades that help you more but are less likely to be accepted. `propose_long_shots = true` makes the bot send those instead of the safe picks.
- `waivers.never_drop`: players it must never cut.
- `lineup.enabled`, `waivers.enabled`, `trades.enabled`: turn pieces off.

## Things to know

- **ESPN has no official API.** This uses the same private endpoints the ESPN website uses. Others have confirmed that lineup changes and free agent adds work in 2026. Waiver claims and trade proposals use the payload format the website sends, but I haven't seen anyone confirm those two live yet. Watch your first live run; if ESPN rejects something, the run fails and GitHub emails you.
- **Cookies expire.** If runs start failing with a 401, grab fresh `espn_s2` / `SWID` values.
- **Projections are ESPN's.** The bot is only as smart as those numbers plus its blending. It doesn't read news, so it won't know about a depth-chart change until ESPN's projections do.
- **Division winners and head-to-head tiebreakers aren't modeled.** Playoff seeding uses wins, then points for.
- **Memory.** It remembers past offers and pickups (to avoid spamming and churning) in the GitHub Actions cache. If the cache is cleared, it just starts fresh.
- **Etiquette.** Automated offers to your friends can get old fast. The defaults are conservative on purpose.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest
```

The layout:

| File | What it does |
|---|---|
| `espn.py` | HTTP client and transaction payloads |
| `models.py` | Parses ESPN's JSON |
| `projections.py` | Expected points per player per week |
| `valuation.py` | Optimal lineups and roster value |
| `simulate.py` | Season and playoff Monte Carlo |
| `waivers.py` | Pickup search |
| `trades.py` | Trade search |
| `engine.py` | Runs it all and executes moves |
| `report.py` | Builds the markdown report and JSON snapshot |
| `demo.py` | Synthetic league for tests and `--demo` |
