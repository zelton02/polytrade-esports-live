# Polytrade Esports Live

An independent, read-only CS2 live probability research project with paper-only
position management. Runtime dependencies: **Python standard library only**.

The project intentionally does not place real orders, hold wallet keys, or expose
an authenticated trading path.

Live board: <https://esports.zhng.tech> (password protected).

## How a probability gets made

```
Polymarket Gamma  --discover-->  open CS2 match markets
                                 teams, Bo, CLOB token ids, pandascoreMatchId
Hermes / DeepSeek --pre-match--> prior_probability_a + written rationale
PandaScore        --live----->   maps, rounds, current map, side
Polymarket CLOB   --book----->   executable bid/ask
                                          |
                                    engine.tick
                                          |
                        forecast + edge vs ask + capped paper position
                                          |
                                   SQLite --> dashboard
```

The two model layers run at different cadences on purpose. An LLM call takes
minutes and costs money, while a CS2 round turns over in about ninety seconds,
so the LLM sets the **pre-match series prior** once per match and the
deterministic engine does every **in-play update** from it.

Until a match has an LLM prior it sits at the neutral seed of 0.50. A seed prior
is the absence of a view, not a 50/50 view, so the paper engine stands down on
those matches: the forecast is still recorded and displayed, but no position is
sized from it.

## What this does

- Discovers open CS2 match markets from the public Polymarket Gamma API, with no
  scraping and no API key.
- Prices upcoming matches with an LLM pre-match prior, under daily and monthly
  cost caps, storing the full rationale and evidence for audit.
- Reads live series state (maps, rounds, current map) from PandaScore, joined by
  the `pandascoreMatchId` that Polymarket itself publishes.
- Falls back to a maps-only state derived from resolved per-map markets when no
  live provider is configured.
- Stores executable Polymarket bid/ask snapshots separately from game state.
- Computes edge against the ask, never against a decorative midpoint.
- Rebalances a capped paper position with entry/exit hysteresis.
- Replays JSONL fixtures deterministically for research and tests.
- Settles finished matches from the Polymarket result and scores the AI prior
  against the market baseline (Brier, log loss, hit rate).
- Tracks fixture lifecycle separately from market settlement: a completed match
  moves to `AWAITING SETTLEMENT` immediately instead of remaining `LIVE` until
  Polymarket resolves it.
- Serves a live dashboard with basic auth and a strict CSP.
- Rejects future-dated source observations to reduce leakage risk.

## Live data sources

| Layer | Source | Key needed |
|---|---|---|
| Match discovery, teams, tokens | Polymarket Gamma `/events?tag_slug=esports` | no |
| Order book (bid/ask) | Polymarket CLOB `/book` | no |
| Maps won, rounds, current map | PandaScore `/csgo/matches/running` | **yes** (free tier) |
| Maps won (fallback) | resolved per-map Gamma markets | no |
| Pre-match prior | DeepSeek API (or Hermes CLI) | `DEEPSEEK_API_KEY` |

Polymarket's own `score` field is present on CS2 events but was observed frozen
at `000-000|0-0|Bo3` on live matches, so it is not used for round state. Its
`live`, `period` and `ended` flags are used.

Set the PandaScore token and confirm what the plan actually exposes:

```bash
export PANDASCORE_TOKEN=...
python3 -m polytrade_esports pandascore-probe
```

Without it, the board still runs: matches, market prices, model probability and
edge are all live; only the in-map round score is missing.

The dashboard labels this degraded mode `MAPS-ONLY FEED`. In that mode the model
updates at map boundaries while the Polymarket order book continues to refresh;
it must not be mistaken for a round-by-round CS2 model.

## Quick start

Python 3.9+ is enough:

```bash
export PYTHONPATH=src
python3 -m polytrade_esports init --db data/esports_live.sqlite3

# find every open CS2 match market without writing anything
python3 -m polytrade_esports discover --db data/esports_live.sqlite3 --dry-run

# one full cycle: discover, read state, read books, forecast
python3 -m polytrade_esports collect --db data/esports_live.sqlite3 --cycles 1

# run continuously
python3 -m polytrade_esports collect --db data/esports_live.sqlite3 --cycles 0 --interval-seconds 60

# price upcoming matches with the LLM (see the candidates first)
python3 -m polytrade_esports forecast-priors --db data/esports_live.sqlite3 --dry-run

python3 -m polytrade_esports serve --db data/esports_live.sqlite3 --port 8788
```

Then open `http://127.0.0.1:8788`.

Set `POLYTRADE_DASHBOARD_USERNAME` and `POLYTRADE_DASHBOARD_PASSWORD_SHA256` to
require a login; with neither set the dashboard is open to whoever can reach the
port, which is why it binds to loopback by default.

Run the tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## The two views

The overview at `/` answers *what is happening across the board*: a grid of
match cards, each carrying only the teams, the score, the model-versus-market
divergence bar, and three figures. Nothing else — an overview that also tries
to be a detail view is neither.

Each card links to `/match/<slug>`, which answers *what happened in this
match*: the probability trajectory over the life of the series, the AI's full
reasoning and cited sources, the current order book, the paper position, and
every recorded observation.

The trajectory chart is the reason the detail page exists. The model's path
against the market over time is not visible anywhere else, and it is what
separates a considered disagreement from the model simply lagging the book.
Map-score changes are marked on it, since those are the moments the model was
supposed to move.

## The stale-model guard

The engine only moves when the state feed moves. Between state changes it is a
constant, so any edge that appears in that window is entirely the market
moving — which means the market has learned something the feed has not shown
us, and the apparent edge is our blindness rather than their error.

The cost of not having this guard, from the recorded history of one match:

```
13:30 - 18:18   model 0.210, score 0-0, unchanged for five hours
                market 0.205 -> 0.185 -> 0.425 -> 0.085
18:18:22        market at 0.085 reads as a +12% edge -> BUY at 0.09
18:19:23        map one ends 0-1, the model finally updates to 0.087
18:35:45        edge gone -> SELL at 0.09
```

The market's collapse to 0.085 *was* map one ending. With no round feed the
model could not see it, so it bought into news it was blind to and sold flat a
minute later. On a real book that round trip costs the spread twice.

Entry is therefore suppressed when the market has drifted more than
`max_market_drift` since the last change in the state's *content*. Exits are
deliberately exempt: being blind is a reason to stop opening positions, never a
reason to keep one you would otherwise close.

The anchor has to follow the state's content, not its row id. Every tick writes
a new state row because `source_at` advances, so keying the anchor on
`state_id` made the drift permanently zero and the guard silently inert — it
was written that way first, and only the tests caught it.

This guard is a mitigation for missing round data, not a substitute for it.
With a live round feed the model would move when the market moves, and the
question would not arise.

## Known follow-ups

- The one-off data repairs in `storage.py` (`_invalidate_ungrounded_priors`,
  `_backfill_grounding`, `_backfill_web_grounding`, `_label_legacy_backends`)
  run on every `initialize()`, which every entry point calls. Each is
  idempotent and cheap, but the right shape is an applied-repairs ledger. That
  changes migration semantics — a fresh database would mark them done
  immediately — so it belongs in its own change with its own tests.
- `web/app.js` and `web/detail.js` duplicate their formatting and DOM helpers.
  The strict CSP forbids inline script and there is no bundler, so a shared
  `util.js` served from the same origin is the fix when the duplication starts
  to drift.
- The Liquipedia throttle is a module-level singleton, so the hourly budget is
  per process. That is correct while the collector, priors and dashboard run as
  separate containers, and wrong the moment they share one.

## Resolution and scoring

A forecast that is never settled is not evidence. Every cycle the collector
periodically re-checks finished matches, reads the winner from the settled
Polymarket series market, pays out any paper position, and adds the match to
the scored cohort.

Fixture state follows `scheduled -> live -> finished_pending -> resolved|void`.
`finished_pending` is displayed as `AWAITING SETTLEMENT`; it is no longer live,
but it remains visible and unscored until the market has a final decision.
When Gamma reports a completed series, the collector writes a separate
`polymarket-gamma-final` map snapshot and never feeds that terminal observation
back into the forecast or paper engine. If the match-winner market is already
decided at 1/0, settlement happens in the same collection cycle; confirmed-ended
fixtures also bypass the generic five-hour polling gate.

```bash
python3 -m polytrade_esports resolve-open --db data/esports_live.sqlite3
python3 -m polytrade_esports score --db data/esports_live.sqlite3 --summary
```

The scoreboard compares two probabilities recorded **at the same instant** on
**the same matches**:

| | meaning |
|---|---|
| **Brier** | mean squared error of the probability; 0.25 is what always saying 50/50 scores, so anything at or above that is worse than not predicting |
| **Log loss** | punishes confident mistakes far harder; a model can win on Brier and lose here |
| **AI prior** | the LLM's pre-match probability |
| **Market baseline** | the book midpoint captured when that prior was written |

Only matches carrying a real LLM prior are scored. A seed prior is the absence
of a view, and scoring it as a prediction would flatter or damn the model with
forecasts it never made. A match whose market baseline was never recorded is
skipped rather than compared against an invented price, and the count of those
is reported.

The report states its own sample size and marks itself unreliable below 30
resolved matches, because an early flattering number is the easiest way to
fool yourself.

### Settlement timing (measured, not assumed)

A match being over is not the same as its market being settled, and scores only
appear after settlement. Across 325 CS2 fixtures sampled on 2026-08-28:

| hours since start | settled |
|---|---|
| 0-6h | **0%** |
| 6-12h | 100% (n=4) |
| 12-24h | 88% |
| 1-2 days | 98% |
| 2-4 days | 98% |
| over 14 days | **5%** |

So a finished match is typically scoreable the next day, not the same evening.
The defaults follow the data: resolution skips anything under 5 hours old
(nothing has ever settled that fast), re-checks every 30 minutes rather than
every cycle, and voids a match after 14 days, past which only 5% ever settle.

Matches more than 12 hours past their start are also not newly tracked, since
Polymarket leaves finished esports events `closed: false` for weeks while UMA
settles, which otherwise drags months of dead fixtures into the database.

## Where the prior gets its facts

The model is not asked to recall or search for team information. It is told,
from data this system fetched and can cite.

That decision came from a controlled test rather than a preference. The first
generation of API-backed priors ran with no web access against a training
cutoff 26 months older than the matches, so the only current information in the
prompt was the market's own machine-written summary. Removing that summary moved
the estimate from 0.27 to 0.46; reversing it moved the estimate to 0.70. The
output was a paraphrase of the market, not a forecast of it. Those 91 priors are
kept in the database and marked `methodology = 'ungrounded...'`, excluded from
scoring, and their matches were reset to the seed so they could be priced again
properly.

`liquipedia.py` fetches, per team:

- the active roster with join dates, which is what makes a stand-in or a
  recent signing visible,
- recent results with dates, tiers, opponents and scores,
- aggregate win rates at match, map and round level.

Only the free MediaWiki API is used; `action=parse` renders the results table,
so no paid tier is required. The published limits are enforced in code, not by
convention, because violating them earns an automated IP ban: one request per
two seconds, one rendered page per thirty seconds, sixty requests per hour, and
a User-Agent that names the project and carries contact details.

Two guards exist because both failures were observed in production:

**Results after the match start are dropped.** Liquipedia publishes a fixture's
result as soon as it is played, so an unfiltered fetch put the result of the
match under forecast inside that match's own evidence. The model noticed and
discounted it, which is exactly the sort of thing that must not be left to the
model.

**A page is refused unless its title plausibly names the team.** Full-text
search returned an ESEA season bracket for "Turma do Pagode" and `Imperial
Female` for "Imperial" — a real team with a real roster, and the wrong one. A
near-miss is a page of facts about somebody else, presented as verified, so
resolution is strict and a team with no usable page simply gets none.

**No facts, no forecast.** When neither team can be grounded the match is
skipped rather than priced. That is the failure the first cohort died of: a
prior with nothing to reason from still looks like a view, still unlocks the
paper engine, and is really just a paraphrase of whatever text was in the
prompt.

## Two prior backends

`forecast-priors --backend deepseek` (default) calls the DeepSeek chat API
directly. It needs no host binary, so it runs inside the collector stack, and it
is cheap enough to price the whole board.

`--backend hermes` shells out to the Hermes CLI with a web toolset.

The difference is not just price, and it shows up in the output:

| | cost/prior | latency | web research | typical confidence |
|---|---|---|---|---|
| `hermes` (deepseek-v4-pro + web) | ~$0.019 | ~3 min | yes | medium |
| `deepseek` / `deepseek-v4-pro` | ~$0.0013 | ~50 s | **no** | low |
| `deepseek` / `deepseek-v4-flash` | ~$0.00008 | ~13 s | **no** | low |

Without web access the model cannot check current form, rosters or head-to-head
records, and it says so: API-backed priors come back at *low* confidence and
lean on the untrusted context blurb Polymarket ships with the event. The prompt
changes to match — a backend with no tools is told it has none, forbidden from
claiming it looked anything up, and told to leave `supporting_evidence` empty
rather than inventing a URL. Telling a model it may research when it cannot is
how you get a confident number behind a fabricated citation.

Every prior records the backend that produced it in its own column, because
`provider` reads "deepseek" either way and cannot separate the cohorts. With
verified facts in the prompt the case for the web-researching backend narrows:
the roster, form and head-to-head it used to go looking for are exactly what
Liquipedia now supplies, and supplies verifiably. Which one is actually better
is a question for the scoreboard, not for this table.

**Caveat worth keeping in view:** that Polymarket blurb is written by
Polymarket's own model and may reflect what the book has already priced. A prior
that leans on it is not fully independent of the market it is being scored
against.

## Cost control

`forecast-priors` enforces a per-run limit, a daily limit, a monthly budget, and
a per-forecast cost cap; it stops rather than overspending. Candidates are ranked
by market liquidity, because a prior on a market too thin to hold a position buys
nothing.

Measured costs are above. On the direct API a prior is ~$0.0013, so the whole
board can be priced for well under a dollar a day; the deployed `priors`
container loops every 15 minutes with `--limit 12 --daily-limit 200` under a
$3/month cap. A match is priced once, so a cycle that finds nothing new costs a
single query and no API call.

Set the cap below the account balance, not above it: the guard stops spending at
the cap, but it cannot stop a call failing when the account is empty.

## Deployment

`compose.yaml` runs three containers: a collector loop, the DeepSeek prior loop,
and a read-only dashboard on `127.0.0.1:8788`. All are unprivileged,
read-only-rootfs and resource-capped. `deploy/` also contains the optional Hermes
prior timer and the Cloudflare tunnel unit.

```bash
chown -R 10002:10002 data      # the container runs as uid 10002
DASH_AUTH=user:password deploy/deploy.sh
```

Use the script rather than `docker compose up -d --build`. On a host without the
buildx plugin `docker compose build` **exits 0 and does nothing**, so the deploy
reports success while the containers keep serving the previous image. The script
builds the tag with `docker build`, recreates, and then compares the sha of the
CSS the server actually returns against the file on disk, because a correct
image proves nothing while a stale container still holds the port.

## Add a real paper match

```bash
python3 -m polytrade_esports add-match \
  --db data/esports_live.sqlite3 \
  --match-id blast-example \
  --team-a NAVI --team-b M80 --best-of 3 \
  --prior-a 0.64 \
  --token-a POLYMARKET_TOKEN_FOR_NAVI \
  --token-b POLYMARKET_TOKEN_FOR_M80
```

Ingest one normalized state plus an observed order book:

```bash
python3 -m polytrade_esports tick \
  --db data/esports_live.sqlite3 \
  --match-id blast-example \
  --source-at 2026-08-28T10:00:00Z \
  --maps-a 1 --maps-b 1 --rounds-a 6 --rounds-b 6 \
  --current-map Inferno \
  --side-advantage-a 0.05 --economy-a 0.4 --economy-b -0.2 \
  --bid-a 0.58 --ask-a 0.59 --bid-b 0.41 --ask-b 0.42
```

Or fetch a public Polymarket book:

```bash
python3 -m polytrade_esports fetch-book --token-id TOKEN_ID
```

For a live provider bridge, pipe normalized state JSONL into `stream`. If each
line contains bid/ask fields they are preserved; otherwise the command fetches
both token books from Polymarket using the match configuration:

```bash
grid_normalizer_command | python3 -m polytrade_esports stream \
  --db data/esports_live.sqlite3 --match-id blast-example -
```

The state provider process can restart independently. Every source timestamp,
local observation timestamp, raw payload, and generated forecast is persisted.

## Normalized JSONL feed

`replay` accepts one JSON object per line. Required values are match state and
both outcome books:

```json
{"source_at":"2026-08-28T10:00:00Z","maps_a":1,"maps_b":1,"rounds_a":6,"rounds_b":6,"current_map":"Inferno","side_advantage_a":0.05,"economy_a":0.4,"economy_b":-0.2,"bid_a":0.58,"ask_a":0.59,"bid_b":0.41,"ask_b":0.42}
```

```bash
python3 -m polytrade_esports replay \
  --db data/esports_live.sqlite3 \
  --match-id blast-example \
  examples/demo_series.jsonl
```

## Model boundary

This V0 is deliberately a transparent state updater, not a trained black box:

1. Convert the pre-match series prior into an implied fresh-map probability.
2. Convert the map probability into an implied per-round probability.
3. Adjust the current round probability for side, economy, and map bias.
4. Calculate current-map win probability from the actual score.
5. Combine current-map and remaining-map probabilities into the series result.

Every forecast stores the full breakdown and model version. Coefficients must be
frozen before evaluating a prospective cohort.

## Data-source boundary

- `PolymarketBookClient`: implemented with `urllib` and the public CLOB REST API.
- Normalized JSONL/manual state: implemented and usable now.
- GRID: recommended official live provider. Exact GraphQL mapping is added after
  access is granted because its schema and credentials are account-scoped.
- HTML scraping is intentionally not part of V0; it is fragile and can violate
  provider terms.

## Research safety

- Paper only. Do not add private keys.
- `source_at` cannot be later than `observed_at`.
- Original state and book timestamps are preserved.
- Replays write to a separate database in serious experiments.
- Live-data delay must be measured; an apparent edge that already exists in the
  market is not tradable alpha.
- Never combine this dataset with the general Polytrade calibration cohort.
