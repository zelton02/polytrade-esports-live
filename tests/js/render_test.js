/* Regression tests for the dashboard renderer.

   The bug these exist for: an earlier version rebuilt both match lists from
   scratch on every five-second poll. Every card replayed its entry animation
   (the board appeared to flash) and any <details> the reader had opened was
   destroyed and recreated closed. */

"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { Document } = require("./dom_stub");

const APP = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "polytrade_esports", "web", "app.js"),
  "utf8"
);

/* boot({ animate: true }) supplies a controllable clock and rAF so the
   counting animation can be stepped deterministically. */
function boot(options) {
  options = options || {};
  const document = new Document();
  const frames = [];
  const clock = { now: 1000 };
  const context = {
    document,
    window: {
      localStorage: (function () {
        const store = options.stored || {};
        return {
          getItem: (k) => (k in store ? store[k] : null),
          setItem: (k, v) => { store[k] = String(v); },
        };
      })(),
    },
    console: { error() {} },
    setInterval() {},
    fetch: () => new Promise(() => {}),
    performance: { now: () => clock.now },
    Promise,
    Date,
    Math,
    Number,
    String,
    Object,
    Array,
    isNaN,
    parseInt,
  };
  if (options.animate) {
    context.requestAnimationFrame = (fn) => { frames.push(fn); };
    context.matchMedia = () => ({ matches: false });
  }
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(APP, context);
  const advance = (ms) => {
    clock.now += ms;
    const due = frames.splice(0, frames.length);
    due.forEach((fn) => fn(clock.now));
  };
  return { context, document, advance, frames };
}

function match(overrides) {
  return Object.assign(
    {
      match_id: "cs2-a-b-2026-08-28",
      team_a: "G2",
      team_b: "Spirit",
      best_of: 3,
      live: 1,
      ended: 0,
      status: "open",
      league: "BLAST",
      serie: "Fall",
      tournament: "Group A",
      scheduled_at: "2026-08-28T11:30:00Z",
      maps_a: 0,
      maps_b: 0,
      rounds_a: 0,
      rounds_b: 0,
      current_map: "Mirage",
      probability_a: 0.22,
      market_midpoint_a: 0.285,
      ask_a: 0.29,
      bid_a: 0.28,
      ask_b: 0.72,
      bid_b: 0.71,
      edge_a: -0.07,
      edge_b: 0.06,
      best_side: "B",
      liquidity: 1234.5,
      prior_source: "llm:deepseek-v4-pro",
      prior_backend: "deepseek",
      prior_grounded_teams: 2,
      prior_backend: "deepseek",
      prior_grounded_teams: 2,
      prior_probability_llm: 0.22,
      prior_confidence: "medium",
      prior_reasoning: "Spirit hold the head-to-head.",
      prior_model: "deepseek-v4-pro",
      prior_created_at: "2026-08-28T09:29:00Z",
      key_factors: ["ranking gap"],
      book_source_at: "2026-08-28T09:30:00Z",
    },
    overrides || {}
  );
}

function payload(matches, overrides) {
  return Object.assign(
    {
      generated_at: "2026-08-28T09:30:05Z",
      latest_forecast_at: new Date().toISOString(),
      counts: { live: 1, pending: 0, matches: 1, priced: 1, forecasts: 12, resolved: 0 },
      scoring: {
        ai: { n: 0, brier: null, log_loss: null, accuracy: null },
        market: { n: 0, brier: null, log_loss: null, accuracy: null },
        brier_edge: null, verdict: "insufficient data", reliable: false,
        resolved_total: 0, missing_baseline: 0, ai_beats_coin_flip: null,
      },
      collector: { finished_at: "2026-08-28T09:30:00Z", ticked: 17, status: "completed" },
      account: { equity: 1000, return: 0, trades: [] },
      matches,
    },
    overrides || {}
  );
}

function anyMarked(node) {
  if (node.classList.contains("moved")) return true;
  return node.children.some(anyMarked);
}

/* Cards are wrapped in a link now, so the article sits one level down. */
function cardOf(node) {
  return node.children[0];
}

function collectText(node, out) {
  out = out || [];
  if (!node.children.length) out.push(node.textContent);
  node.children.forEach(function (child) { collectText(child, out); });
  return out;
}

function byClass(node, className) {
  var wanted = className.split(" ");
  var has = wanted.every(function (c) { return node.classList.contains(c); });
  if (has) return node;
  for (var i = 0; i < node.children.length; i += 1) {
    var found = byClass(node.children[i], className);
    if (found) return found;
  }
  return null;
}

function figureValue(card, label) {
  var figures = byClass(card, "figures");
  for (var i = 0; i < figures.children.length; i += 1) {
    var cell = figures.children[i];
    if (cell.children[0].textContent === label) return cell.children[1];
  }
  return null;
}

function textOf(card, className) {
  var node = byClass(card, className);
  return node ? node.textContent : null;
}

const tests = {
  "card node survives a refresh instead of being rebuilt"() {
    const { context, document } = boot();
    context.render(payload([match()]));
    const first = document.getElementById("live-list").children[0];
    context.render(payload([match()]));
    const second = document.getElementById("live-list").children[0];
    assert.ok(first === second, "card was recreated on refresh");
  },

  "entry animation class is dropped and never reapplied"() {
    const { context, document } = boot();
    context.render(payload([match()]));
    const card = cardOf(document.getElementById("live-list").children[0]);
    assert.ok(card.classList.contains("enter"), "new card should animate in");
    card.dispatch("animationend");
    assert.ok(!card.classList.contains("enter"), "class should clear after animating");
    context.render(payload([match({ probability_a: 0.31 })]));
    assert.ok(!card.classList.contains("enter"), "refresh must not replay the animation");
  },

  "changed values are written, steady values are not touched"() {
    const { context, document } = boot();
    context.render(payload([match()]));
    const card = cardOf(document.getElementById("live-list").children[0]);
    const teamNode = byClass(card, "team");
    assert.strictEqual(teamNode.textContent, "G2");
    let writes = 0;
    Object.defineProperty(teamNode, "textContent", {
      get() { return "G2"; },
      set() { writes += 1; },
    });
    context.render(payload([match({ probability_a: 0.9 })]));
    assert.strictEqual(writes, 0, "unchanged team name must not be rewritten");
  },

  "a card whose numbers moved is highlighted"() {
    const { context, document } = boot();
    context.render(payload([match()]));
    const card = cardOf(document.getElementById("live-list").children[0]);
    assert.ok(!card.classList.contains("moved"), "first paint must not highlight");

    context.render(payload([match({ probability_a: 0.55 })]));
    assert.ok(card.classList.contains("moved"), "a moved value should light the card");
    assert.strictEqual(figureValue(card, "MODEL").textContent, "55.0%");
  },

  "book age is shown so a quiet card still looks alive"() {
    const { context, document } = boot();
    const stamp = new Date(Date.now() - 20000).toISOString();
    context.render(payload([match({ book_source_at: stamp })]));
    const card = cardOf(document.getElementById("live-list").children[0]);
    const meta = byClass(card, "meta").children[1];
    assert.ok(/2[0-9]s/.test(meta.textContent), "age should be ~20s: " + meta.textContent);
  },

  "order is stable when only the forecast timestamp moves"() {
    const { context, document } = boot();
    const early = match({
      match_id: "cs2-early", team_a: "Early", scheduled_at: "2026-08-28T09:00:00Z",
    });
    const later = match({
      match_id: "cs2-later", team_a: "Later", scheduled_at: "2026-08-28T15:00:00Z",
    });
    // The backend used to order by newest forecast, which is rewritten every
    // tick. The board must not care.
    context.render(payload([early, later]));
    const list = document.getElementById("live-list");
    const first = list.children[0];
    assert.strictEqual(byClass(first, "team").textContent, "Early");

    context.render(payload([later, early]));
    assert.strictEqual(list.children[0], first, "cards must not swap places");
    assert.strictEqual(byClass(list.children[0], "team").textContent, "Early");
  },

  "a new match lands in schedule order, not at the end"() {
    const { context, document } = boot();
    const late = match({ match_id: "cs2-late", team_a: "Late", scheduled_at: "2026-08-28T18:00:00Z" });
    const mid = match({ match_id: "cs2-mid", team_a: "Mid", scheduled_at: "2026-08-28T12:00:00Z" });
    context.render(payload([late]));
    context.render(payload([late, mid]));
    const list = document.getElementById("live-list");
    assert.strictEqual(byClass(list.children[0], "team").textContent, "Mid");
    assert.strictEqual(byClass(list.children[1], "team").textContent, "Late");
  },

  "divergence band spans model to market"() {
    const { context, document } = boot();
    context.render(payload([match({ probability_a: 0.22, market_midpoint_a: 0.42 })]));
    const card = cardOf(document.getElementById("live-list").children[0]);
    const band = byClass(card, "band");
    assert.strictEqual(band.style.left, "22.00%");
    assert.strictEqual(band.style.width, "20.00%");
    assert.ok(band.className.indexOf("negative") >= 0, "model below market reads negative");
    assert.strictEqual(byClass(card, "mark model").style.left, "22.00%");
    assert.strictEqual(byClass(card, "mark market").style.left, "42.00%");
  },

  "seed model is drawn differently from a priced one"() {
    const { context, document } = boot({ stored: { "polytrade.aiPricedOnly": "0" } });
    context.render(payload([match({ prior_source: "seed" })]));
    const card = cardOf(document.getElementById("live-list").children[0]);
    assert.ok(byClass(card, "mark model").className.indexOf("seed") >= 0);
  },

  "scoreboard says nothing until a priced match resolves"() {
    const { context, document } = boot();
    context.render(payload([match()]));
    assert.ok(
      /AWAITING/.test(document.getElementById("verdict").textContent),
      "an empty scoreboard must not imply a result"
    );
    assert.strictEqual(document.getElementById("ai-brier").textContent, "—");
  },

  "a quiet poll highlights nothing"() {
    const { context, document } = boot();
    context.render(payload([match()]));
    context.render(payload([match()]));
    const card = cardOf(document.getElementById("live-list").children[0]);
    assert.ok(!card.classList.contains("moved"), "an unchanged poll must stay dark");
  },

  "scoreboard names the winner and flags a small sample"() {
    const { context, document } = boot();
    const data = payload([match()]);
    data.scoring = {
      ai: { n: 4, brier: 0.18, log_loss: 0.52, accuracy: 0.75 },
      market: { n: 4, brier: 0.24, log_loss: 0.63, accuracy: 0.5 },
      brier_edge: 0.06, reliable: false, resolved_total: 4,
      missing_baseline: 0, ai_beats_coin_flip: true,
    };
    context.render(data);
    const verdict = document.getElementById("verdict");
    assert.ok(/AI AHEAD/.test(verdict.textContent), verdict.textContent);
    assert.ok(verdict.className.indexOf("ahead") >= 0);
    assert.strictEqual(document.getElementById("ai-brier").textContent, "0.180");
    assert.ok(
      /too small/.test(document.getElementById("caveat").textContent),
      "a 4-match sample must be flagged"
    );
  },

  "scoreboard does not hide a losing model"() {
    const { context, document } = boot();
    const data = payload([match()]);
    data.scoring = {
      ai: { n: 40, brier: 0.29, log_loss: 0.9, accuracy: 0.45 },
      market: { n: 40, brier: 0.21, log_loss: 0.6, accuracy: 0.65 },
      brier_edge: -0.08, reliable: true, resolved_total: 40,
      missing_baseline: 0, ai_beats_coin_flip: false,
    };
    context.render(data);
    const verdict = document.getElementById("verdict");
    assert.ok(/MARKET AHEAD/.test(verdict.textContent), verdict.textContent);
    assert.strictEqual(
      document.getElementById("coin-note").textContent,
      "AI does not beat it"
    );
  },

  "a number travels to its new value instead of teleporting"() {
    const { context, document, advance } = boot({ animate: true });
    context.render(payload([match({ probability_a: 0.20 })]));
    const card = cardOf(document.getElementById("live-list").children[0]);
    const model = figureValue(card, "MODEL");
    assert.strictEqual(model.textContent, "20.0%", "first paint is immediate");

    context.render(payload([match({ probability_a: 0.80 })]));
    assert.strictEqual(model.textContent, "20.0%", "no frame has run yet");

    advance(200);
    const mid = parseFloat(model.textContent);
    assert.ok(mid > 20 && mid < 80, "should be mid-flight, got " + model.textContent);

    advance(1000);
    assert.strictEqual(model.textContent, "80.0%", "must land exactly on target");
  },

  "a value changing mid-flight retargets rather than stacking"() {
    const { context, document, advance } = boot({ animate: true });
    context.render(payload([match({ probability_a: 0.20 })]));
    const card = cardOf(document.getElementById("live-list").children[0]);
    const model = figureValue(card, "MODEL");

    context.render(payload([match({ probability_a: 0.80 })]));
    advance(150);
    context.render(payload([match({ probability_a: 0.50 })]));
    advance(1000);
    assert.strictEqual(model.textContent, "50.0%", "the latest value must win");
  },

  "a finished match moves from live to awaiting settlement"() {
    const { context, document } = boot();
    context.render(payload([match()]));
    context.render(payload([match({ live: 1, ended: 1, status: "open" })], {
      counts: { live: 0, pending: 1, matches: 1, priced: 1, resolved: 0 },
    }));
    assert.strictEqual(document.getElementById("c-live").textContent, "[0]");
    assert.strictEqual(document.getElementById("c-pending").textContent, "[1]");
    assert.strictEqual(document.getElementById("live-list").children[0].className, "empty");
    const pending = cardOf(document.getElementById("pending-list").children[0]);
    assert.strictEqual(textOf(pending, "tag pending"), "PENDING");
  },

  "a stale live flag cannot resurrect a resolved match"() {
    const { context, document } = boot();
    context.render(payload([match({ live: 1, ended: 1, status: "resolved" })]));
    assert.strictEqual(document.getElementById("live-list").children[0].className, "empty");
    assert.strictEqual(document.getElementById("pending-list").children[0].className, "empty");
  },

  "maps-only provider mode is visible in the feed beacon"() {
    const { context, document } = boot();
    const data = payload([match()]);
    data.collector.notices = ["round-level data unavailable; model updates are maps-only"];
    context.render(data);
    assert.strictEqual(document.getElementById("live-label").textContent, "MAPS-ONLY FEED");
    assert.ok(document.getElementById("live-dot").className.indexOf("stale") >= 0);
  },

  "the card separates a grounded prior from an ungrounded one"() {
    const { context, document } = boot({ stored: { "polytrade.aiPricedOnly": "0" } });
    context.render(payload([match()]));
    let badge = byClass(cardOf(document.getElementById("live-list").children[0]), "prior-badge");
    assert.ok(/GROUNDED/.test(badge.textContent), badge.textContent);
    assert.ok(badge.className.indexOf("grounded") >= 0);
    assert.ok(
      badge.textContent.indexOf("head-to-head") < 0,
      "the reasoning belongs on the detail page, not the overview"
    );

    // A prior written without fetched facts is a different object and must
    // not read the same on the board.
    context.render(payload([match({ prior_grounded_teams: 0 })]));
    badge = byClass(cardOf(document.getElementById("live-list").children[0]), "prior-badge");
    assert.ok(/AI PRIOR/.test(badge.textContent), badge.textContent);
    assert.ok(badge.className.indexOf("grounded") < 0, "ungrounded must not read as grounded");

    context.render(payload([match({ prior_source: "seed", prior_grounded_teams: 0 })]));
    badge = byClass(cardOf(document.getElementById("live-list").children[0]), "prior-badge");
    assert.ok(/SEED 50%/.test(badge.textContent), "the seed must be named as an absence of a view");
  },

  "the scoreboard admits what it excluded"() {
    const { context, document } = boot();
    const data = payload([match()]);
    data.scoring = {
      ai: { n: 2, brier: 0.19, log_loss: 0.55, accuracy: 1.0 },
      market: { n: 2, brier: 0.21, log_loss: 0.6, accuracy: 0.5 },
      brier_edge: 0.02, reliable: false, resolved_total: 2,
      missing_baseline: 0, ai_beats_coin_flip: true, excluded: 91,
    };
    context.render(data);
    const caveat = document.getElementById("caveat").textContent;
    assert.ok(/91 earlier priors are excluded/.test(caveat), caveat);
    assert.ok(/summary/.test(caveat), "the reason for exclusion must be stated");
  },

  "an unticked match shows no reading rather than a reading of zero"() {
    // The markers used to default to 0%, which pinned them to the left edge
    // and read as "the model says 0%" on every upcoming match.
    const { context, document } = boot({ stored: { "polytrade.aiPricedOnly": "0" } });
    context.render(payload([match({
      probability_a: null, market_midpoint_a: null, edge_a: null, edge_b: null,
      prior_probability_llm: null, prior_source: "seed",
    })]));
    const card = cardOf(document.getElementById("live-list").children[0]);
    assert.strictEqual(byClass(card, "mark model").style.display, "none");
    assert.strictEqual(byClass(card, "mark market").style.display, "none");
    assert.ok(byClass(card, "track").className.indexOf("idle") >= 0);
    assert.strictEqual(byClass(card, "track-note").textContent, "AWAITING FIRST TICK");
    assert.strictEqual(figureValue(card, "MODEL").textContent, "—");
  },

  "a priced match shows its prior before the first tick"() {
    const { context, document } = boot();
    context.render(payload([match({
      probability_a: null, market_midpoint_a: null,
      prior_probability_llm: 0.72, prior_source: "llm:deepseek-v4-pro",
    })]));
    const card = cardOf(document.getElementById("live-list").children[0]);
    assert.strictEqual(figureValue(card, "MODEL").textContent, "72.0%");
    assert.strictEqual(byClass(card, "mark model").style.display, "");
    assert.strictEqual(byClass(card, "mark model").style.left, "72.00%");
    // The market has no current book, so it stays absent rather than borrowing
    // the price recorded when the prior was written.
    assert.strictEqual(byClass(card, "mark market").style.display, "none");
    assert.strictEqual(figureValue(card, "MARKET").textContent, "—");
  },

  "markers reappear once a forecast arrives"() {
    const { context, document } = boot();
    context.render(payload([match({ probability_a: null, market_midpoint_a: null,
                                    prior_probability_llm: null })]));
    const card = cardOf(document.getElementById("live-list").children[0]);
    assert.strictEqual(byClass(card, "mark model").style.display, "none");
    context.render(payload([match()]));
    assert.strictEqual(byClass(card, "mark model").style.display, "");
    assert.ok(byClass(card, "track").className.indexOf("idle") < 0);
    assert.strictEqual(byClass(card, "track-note").textContent, "");
  },

  "matches with no AI prior are hidden by default"() {
    const { context, document } = boot();
    context.render(payload([
      match({ match_id: "priced" }),
      match({ match_id: "unpriced", prior_source: "seed", prior_grounded_teams: 0 }),
    ]));
    const list = document.getElementById("live-list");
    assert.strictEqual(list.children.length, 1, "only the priced match should show");
    assert.strictEqual(document.getElementById("c-live").textContent, "[1/2]");
  },

  "the board says how many it is hiding"() {
    const { context, document } = boot();
    context.render(payload([
      match({ match_id: "priced" }),
      match({ match_id: "unpriced", prior_source: "seed" }),
    ]));
    const note = document.getElementById("filter-note").textContent;
    assert.ok(/1 matches hidden/.test(note), note);
    assert.ok(/seed 50%/.test(note), "the reason must be stated: " + note);
  },

  "turning the filter off reveals the rest without waiting for a poll"() {
    const { context, document } = boot();
    context.render(payload([
      match({ match_id: "priced" }),
      match({ match_id: "unpriced", prior_source: "seed" }),
    ]));
    const list = document.getElementById("live-list");
    assert.strictEqual(list.children.length, 1);

    document.getElementById("filter-toggle").dispatch("click");
    assert.strictEqual(list.children.length, 2, "both matches should show");
    assert.strictEqual(document.getElementById("filter-label").textContent, "SHOWING ALL");
    assert.strictEqual(
      document.getElementById("filter-toggle").getAttribute("aria-pressed"), "false"
    );
  },

  "a remembered choice survives a reload"() {
    const { context, document } = boot({ stored: { "polytrade.aiPricedOnly": "0" } });
    context.render(payload([
      match({ match_id: "priced" }),
      match({ match_id: "unpriced", prior_source: "seed" }),
    ]));
    assert.strictEqual(document.getElementById("live-list").children.length, 2);
    assert.strictEqual(document.getElementById("filter-label").textContent, "SHOWING ALL");
  },

  "an empty filtered section explains why it is empty"() {
    const { context, document } = boot();
    context.render(payload([match({ match_id: "unpriced", prior_source: "seed" })]));
    const empty = document.getElementById("live-list").children[0];
    assert.strictEqual(empty.className, "empty");
    assert.ok(/Turn the filter off/.test(empty.textContent), empty.textContent);
  },

  "a filtered-out settlement backlog is still reported"() {
    // Saying "none are awaiting settlement" while two dozen are stuck would
    // hide a stalled settlement pipeline behind an analysis filter.
    const { context, document } = boot();
    context.render(payload([
      match({ match_id: "done-1", live: 0, ended: 1, status: "open", prior_source: "seed" }),
      match({ match_id: "done-2", live: 0, ended: 1, status: "open", prior_source: "seed" }),
    ]));
    const empty = document.getElementById("pending-list").children[0];
    assert.strictEqual(empty.className, "empty");
    assert.ok(/2 finished matches are waiting/.test(empty.textContent), empty.textContent);
    assert.ok(
      !/No finished matches are awaiting/.test(empty.textContent),
      "the placeholder must not deny a backlog that exists"
    );
    assert.strictEqual(document.getElementById("c-pending").textContent, "[0/2]");
  },

  "a genuinely empty settlement queue still says so"() {
    const { context, document } = boot();
    context.render(payload([match()]));
    const empty = document.getElementById("pending-list").children[0];
    assert.ok(/No finished matches are awaiting/.test(empty.textContent), empty.textContent);
  },

  "empty list shows a placeholder and recovers"() {
    const { context, document } = boot();
    context.render(payload([]));
    const list = document.getElementById("live-list");
    assert.strictEqual(list.children.length, 1);
    assert.strictEqual(list.children[0].className, "empty");
    context.render(payload([match()]));
    assert.strictEqual(list.children.length, 1);
    assert.strictEqual(list.children[0].className.indexOf("match"), 0);
  },
};

let failed = 0;
Object.keys(tests).forEach((name) => {
  try {
    tests[name]();
    console.log("ok   - " + name);
  } catch (error) {
    failed += 1;
    console.log("FAIL - " + name + "\n       " + error.message);
  }
});
console.log("\n" + (Object.keys(tests).length - failed) + "/" + Object.keys(tests).length + " passed");
process.exit(failed ? 1 : 0);
