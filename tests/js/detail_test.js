/* Tests for the match detail page: the chart it draws and the panels that
   have to stay honest when data is missing. */

"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { Document, Element } = require("./dom_stub");

const APP = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "polytrade_esports", "web", "detail.js"),
  "utf8"
);

function boot(pathname) {
  const document = new Document();
  document.createElementNS = function (ns, tag) {
    const node = new Element(tag);
    node.namespace = ns;
    return node;
  };
  const context = {
    document,
    window: { location: { pathname: pathname || "/match/cs2-g2-ts7-2026-08-28" }, console: {} },
    console: { error() {} },
    setInterval() {},
    fetch: () => new Promise(() => {}),
    Promise, Date, Math, Number, String, Object, Array, JSON,
    isNaN, parseInt, parseFloat, encodeURIComponent, decodeURIComponent,
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(APP, context);
  return { context, document };
}

function point(overrides) {
  return Object.assign(
    {
      forecast_at: "2026-08-28T09:30:00Z",
      probability_a: 0.5,
      market_midpoint_a: 0.5,
      edge_a: 0, edge_b: 0, best_side: null,
      maps_a: 0, maps_b: 0, rounds_a: 0, rounds_b: 0,
      current_map: "MAP 1", state_source: "pandascore",
      bid_a: 0.49, ask_a: 0.51, bid_b: 0.49, ask_b: 0.51,
    },
    overrides || {}
  );
}

function detail(overrides) {
  return Object.assign(
    {
      match_id: "cs2-g2-ts7-2026-08-28",
      team_a: "G2", team_b: "Spirit", best_of: 3,
      league: "BLAST", serie: "Fall", tournament: "Group A",
      scheduled_at: "2026-08-28T11:30:00Z",
      status: "open", live: 1, ended: 0, winner: null, liquidity: 300000,
      prior_source: "llm:deepseek-v4-pro",
      prior: {
        probability_a: 0.22, confidence: "medium", model: "deepseek-v4-pro",
        market_probability_a: 0.285, created_at: "2026-08-28T09:24:00Z",
        reasoning_summary: "Spirit hold the head-to-head.",
        key_factors: ["ranking gap"],
        evidence: [{ title: "Report", url: "https://example.org/a" }],
      },
      history: [point(), point({ probability_a: 0.22, market_midpoint_a: 0.285 })],
      latest: point({ probability_a: 0.22, market_midpoint_a: 0.285, edge_a: -0.07, edge_b: 0.06, best_side: "B" }),
      positions: [], trades: [],
    },
    overrides || {}
  );
}

function find(node, className, out) {
  out = out || [];
  if (node.classList && node.classList.contains(className)) out.push(node);
  (node.children || []).forEach((c) => find(c, className, out));
  return out;
}

const tests = {
  "chart draws a model line and a market line"() {
    const { context, document } = boot();
    context.render(detail());
    const chart = document.getElementById("chart");
    assert.strictEqual(find(chart, "series-model").length, 1, "model line missing");
    assert.strictEqual(find(chart, "series-market").length, 1, "market line missing");
    assert.strictEqual(find(chart, "series-band").length, 1, "divergence band missing");
  },

  "a single observation is refused rather than drawn as a line"() {
    const { context, document } = boot();
    context.render(detail({ history: [point()] }));
    const chart = document.getElementById("chart");
    assert.strictEqual(find(chart, "series-model").length, 0);
    assert.ok(/Not enough observations/.test(chart.textContent), chart.textContent);
  },

  "map score changes are marked on the chart"() {
    const { context, document } = boot();
    context.render(detail({
      history: [
        point({ maps_a: 0, maps_b: 0 }),
        point({ maps_a: 1, maps_b: 0, probability_a: 0.75 }),
        point({ maps_a: 1, maps_b: 0, probability_a: 0.76 }),
      ],
    }));
    const marks = find(document.getElementById("chart"), "event-line");
    assert.strictEqual(marks.length, 1, "exactly one map change should be marked");
  },

  "an unscoreable prior says so instead of hiding it"() {
    const { context, document } = boot();
    const d = detail();
    d.prior.market_probability_a = null;
    context.render(d);
    const text = document.getElementById("d-ai").textContent;
    assert.ok(/cannot be scored/.test(text), text);
  },

  "a seed match explains why there is no position"() {
    const { context, document } = boot();
    context.render(detail({ prior_source: "seed", prior: null, positions: [], trades: [] }));
    const paper = document.getElementById("d-paper").textContent;
    assert.ok(/stands down/.test(paper), paper);
    const ai = document.getElementById("d-ai").textContent;
    assert.ok(/neutral seed/.test(ai), ai);
  },

  "a priced match with no position blames the threshold, not the prior"() {
    const { context, document } = boot();
    context.render(detail({ positions: [], trades: [] }));
    const paper = document.getElementById("d-paper").textContent;
    assert.ok(/entry threshold/.test(paper), paper);
  },

  "external evidence links cannot reach back through the opener"() {
    const { context, document } = boot();
    context.render(detail());
    const links = [];
    (function walk(n) {
      if (n.tagName === "A") links.push(n);
      (n.children || []).forEach(walk);
    })(document.getElementById("d-ai"));
    assert.ok(links.length >= 1, "evidence link missing");
    links.forEach((a) => {
      assert.ok(/noopener/.test(a.rel), "missing noopener: " + a.rel);
      assert.ok(/noreferrer/.test(a.rel), "missing noreferrer: " + a.rel);
    });
  },

  "a resolved match names the winner"() {
    const { context, document } = boot();
    context.render(detail({ status: "resolved", winner: "B", live: 0 }));
    const state = document.getElementById("d-state").textContent;
    assert.ok(/RESOLVED/.test(state) && /Spirit/.test(state), state);
  },

  "a finished unsettled match says it is awaiting settlement"() {
    const { context, document } = boot();
    context.render(detail({ status: "open", live: 0, ended: 1 }));
    const state = document.getElementById("d-state").textContent;
    assert.ok(/FINISHED/.test(state) && /AWAITING SETTLEMENT/.test(state), state);
  },

  "observations table is newest first"() {
    const { context, document } = boot();
    context.render(detail({
      history: [
        point({ forecast_at: "2026-08-28T09:00:00Z", probability_a: 0.10 }),
        point({ forecast_at: "2026-08-28T10:00:00Z", probability_a: 0.90 }),
      ],
    }));
    const body = document.getElementById("d-obs");
    assert.strictEqual(body.children[0].children[1].textContent, "90.0%");
  },

  "a missing match id fails loudly"() {
    const { context, document } = boot("/match/");
    context.refresh();
    assert.strictEqual(document.getElementById("d-state").textContent, "NO MATCH IN URL");
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
