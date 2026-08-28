/* Match detail page.

   The overview answers "what is happening across the board"; this page answers
   "what happened in this match". The trajectory chart is the reason it exists:
   the model's path against the market over the life of the series is not
   visible anywhere else, and it is the only way to see whether a disagreement
   was a considered view or a lag.

   Chart is hand-drawn SVG. The page's CSP forbids external scripts, and a
   two-series line does not need a library. */

"use strict";

var REFRESH_MS = 10000;
var SVG_NS = "http://www.w3.org/2000/svg";
var VIEW_W = 1000;
var VIEW_H = 300;
var PAD = { top: 14, right: 14, bottom: 26, left: 40 };

function matchIdFromPath() {
  var parts = window.location.pathname.split("/").filter(Boolean);
  return parts.length > 1 ? decodeURIComponent(parts[parts.length - 1]) : "";
}

/* Formatting ------------------------------------------------------------- */

function pct(v) {
  return v === null || v === undefined || isNaN(v) ? "—" : (Number(v) * 100).toFixed(1) + "%";
}
function signedPct(v) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  var n = Number(v) * 100;
  return (n >= 0 ? "+" : "") + n.toFixed(1) + "%";
}
function money(v) {
  return v === null || v === undefined || isNaN(v) ? "—" : "$" + Number(v).toFixed(2);
}
function clock(iso) {
  if (!iso) return "—";
  var d = new Date(iso);
  return isNaN(d.getTime()) ? "—" : d.toLocaleTimeString([], { hour12: false });
}
function stamp(iso) {
  if (!iso) return "—";
  var d = new Date(iso);
  return isNaN(d.getTime()) ? "—" : d.toLocaleString([], { hour12: false });
}
function num(v, fallback) {
  return v === null || v === undefined ? (fallback || "—") : v;
}
function tone(v) {
  if (v === null || v === undefined || isNaN(v)) return "flat";
  return Number(v) > 0.02 ? "up" : Number(v) < -0.02 ? "down" : "flat";
}

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function svg(tag, attrs) {
  var node = document.createElementNS(SVG_NS, tag);
  Object.keys(attrs || {}).forEach(function (key) {
    node.setAttribute(key, String(attrs[key]));
  });
  return node;
}
function setText(node, value) {
  var next = String(value);
  if (node.textContent !== next) node.textContent = next;
}
function setClass(node, value) {
  if (node.className !== value) node.className = value;
}

/* Chart ------------------------------------------------------------------ */

function drawChart(history) {
  var host = document.getElementById("chart");
  host.textContent = "";

  var points = history.filter(function (p) {
    return p.probability_a !== null && p.probability_a !== undefined;
  });
  if (points.length < 2) {
    host.appendChild(
      el("div", "hint", "Not enough observations yet to draw a trajectory. Points appear once the collector has ticked this match at least twice.")
    );
    return;
  }

  var innerW = VIEW_W - PAD.left - PAD.right;
  var innerH = VIEW_H - PAD.top - PAD.bottom;
  // X is the observation index, not wall-clock: collector cycles are evenly
  // spaced, and index spacing keeps a gap in coverage from stretching the line
  // into a shape the data does not support.
  var x = function (i) { return PAD.left + (i / (points.length - 1)) * innerW; };
  var y = function (p) { return PAD.top + (1 - Math.max(0, Math.min(1, p))) * innerH; };

  var root = svg("svg", { viewBox: "0 0 " + VIEW_W + " " + VIEW_H, preserveAspectRatio: "none" });

  [0, 0.25, 0.5, 0.75, 1].forEach(function (level) {
    root.appendChild(svg("line", {
      class: "grid-line", x1: PAD.left, x2: VIEW_W - PAD.right, y1: y(level), y2: y(level),
    }));
    var label = svg("text", { class: "axis-text", x: 4, y: y(level) + 3 });
    label.textContent = (level * 100).toFixed(0) + "%";
    root.appendChild(label);
  });

  var hasMarket = points.some(function (p) {
    return p.market_midpoint_a !== null && p.market_midpoint_a !== undefined;
  });

  if (hasMarket) {
    // Shaded gap between the two series: the visible disagreement.
    var top = [];
    var bottom = [];
    points.forEach(function (p, i) {
      var m = p.market_midpoint_a;
      if (m === null || m === undefined) m = p.probability_a;
      top.push(x(i) + "," + y(Math.max(p.probability_a, m)));
      bottom.push(x(i) + "," + y(Math.min(p.probability_a, m)));
    });
    root.appendChild(svg("polygon", {
      class: "series-band", points: top.concat(bottom.reverse()).join(" "),
    }));
    root.appendChild(svg("polyline", {
      class: "series-market",
      points: points.map(function (p, i) {
        var m = p.market_midpoint_a;
        return x(i) + "," + y(m === null || m === undefined ? p.probability_a : m);
      }).join(" "),
    }));
  }

  root.appendChild(svg("polyline", {
    class: "series-model",
    points: points.map(function (p, i) { return x(i) + "," + y(p.probability_a); }).join(" "),
  }));

  // Mark where the map score changed: the moments the model was supposed to move.
  points.forEach(function (p, i) {
    if (i === 0) return;
    var before = points[i - 1];
    if (p.maps_a === before.maps_a && p.maps_b === before.maps_b) return;
    root.appendChild(svg("line", {
      class: "event-line", x1: x(i), x2: x(i), y1: PAD.top, y2: PAD.top + innerH,
    }));
    var tag = svg("text", { class: "event-text", x: x(i) + 3, y: PAD.top + 10 });
    tag.textContent = p.maps_a + "-" + p.maps_b;
    root.appendChild(tag);
  });

  var first = svg("text", { class: "axis-text", x: PAD.left, y: VIEW_H - 8 });
  first.textContent = clock(points[0].forecast_at);
  root.appendChild(first);
  var last = svg("text", {
    class: "axis-text", x: VIEW_W - PAD.right, y: VIEW_H - 8, "text-anchor": "end",
  });
  last.textContent = clock(points[points.length - 1].forecast_at);
  root.appendChild(last);

  host.appendChild(root);

  var move = points[points.length - 1].probability_a - points[0].probability_a;
  setText(
    document.getElementById("chart-note"),
    points.length + " OBSERVATIONS · MODEL MOVED " + signedPct(move)
  );
}

/* Panels ----------------------------------------------------------------- */

function kv(pairs) {
  var list = el("dl", "kv");
  pairs.forEach(function (pair) {
    list.appendChild(el("dt", null, pair[0]));
    list.appendChild(el("dd", pair[2] || null, pair[1]));
  });
  return list;
}

function renderAi(detail) {
  var host = document.getElementById("d-ai");
  host.textContent = "";
  var prior = detail.prior;
  if (!prior) {
    host.appendChild(
      el("div", "hint",
        "No AI prior. The model is held at the neutral seed of 50%, and the paper engine will not size a position from it. Priors are written for the most liquid upcoming matches, within a fixed monthly budget.")
    );
    return;
  }
  host.appendChild(
    kv([
      ["PROBABILITY", pct(prior.probability_a) + " " + detail.team_a],
      ["CONFIDENCE", String(prior.confidence || "").toUpperCase()],
      ["MARKET THEN", prior.market_probability_a === null || prior.market_probability_a === undefined
        ? "not recorded — this match cannot be scored"
        : pct(prior.market_probability_a)],
      ["MODEL", prior.model || "—"],
      ["WRITTEN", stamp(prior.created_at)],
    ])
  );
  host.appendChild(el("div", "reasoning", prior.reasoning_summary));

  var factors = prior.key_factors || [];
  if (factors.length) {
    var list = el("ul", "factors");
    factors.forEach(function (f) { list.appendChild(el("li", null, f)); });
    host.appendChild(list);
  }

  var evidence = (prior.evidence || []).filter(function (e) { return e && e.url; });
  if (evidence.length) {
    var box = el("div", "evidence");
    box.appendChild(el("div", "hint", "SOURCES CITED BY THE MODEL"));
    var ul = el("ul", "factors");
    evidence.slice(0, 8).forEach(function (item) {
      var li = el("li");
      var a = el("a", null, item.title || item.url);
      a.href = item.url;
      a.target = "_blank";
      // Untrusted third-party links: never hand them a window opener.
      a.rel = "noopener noreferrer nofollow";
      li.appendChild(a);
      ul.appendChild(li);
    });
    box.appendChild(ul);
    host.appendChild(box);
  }
}

/* The exact block handed to the model, so a reader can check the stated
   reasoning against its inputs rather than trusting the summary.

   Three states, and they are not interchangeable: no prior at all, a prior
   whose evidence was not retained, and a prior with its evidence. Collapsing
   the middle case into the first told the reader "no prior was written" on a
   page that was displaying the prior directly above. */
function renderFacts(detail) {
  var host = document.getElementById("d-facts");
  host.textContent = "";
  var prior = detail.prior;

  if (!prior) {
    host.appendChild(
      el("div", "hint",
        "No prior was written for this match. Liquipedia has no usable page for at least one of these teams, and a forecast with nothing to reason from is worse than none: it would read as a view and would unlock the paper engine.")
    );
    return;
  }

  var block = prior.verified_facts;
  var grounded = prior.grounded_teams || 0;

  if (!block || !String(block).trim()) {
    host.appendChild(
      el("div", "hint",
        grounded > 0
          ? "This prior was written from fetched Liquipedia data — " + grounded +
            " of 2 teams grounded — but predates the change that stores the evidence block, so the exact text is not recoverable. Priors written from now on keep it."
          : "No evidence block was stored with this prior, and it is not recorded as grounded. Treat its reasoning as unverified.")
    );
    return;
  }

  host.appendChild(
    el("div", "hint",
      "Fetched from Liquipedia before the scheduled start. " + grounded +
      " of 2 teams grounded. Results at or after kick-off are excluded.")
  );
  host.appendChild(el("pre", "facts", block));
}

function renderBook(detail) {
  var host = document.getElementById("d-book");
  host.textContent = "";
  var latest = detail.latest;
  if (!latest || latest.ask_a === null || latest.ask_a === undefined) {
    host.appendChild(el("div", "hint", "No order book recorded yet. Books are captured once a match is inside the tracking window."));
    return;
  }
  var wrap = el("div", "book-side");
  [[detail.team_a, latest.bid_a, latest.ask_a], [detail.team_b, latest.bid_b, latest.ask_b]]
    .forEach(function (side) {
      var cell = el("div");
      cell.appendChild(el("div", "who", side[0]));
      cell.appendChild(el("div", "quote", pct(side[1]) + " / " + pct(side[2])));
      cell.appendChild(el("div", "spread", "BID / ASK · SPREAD " + pct(side[2] - side[1])));
      wrap.appendChild(cell);
    });
  host.appendChild(wrap);
  host.appendChild(
    kv([
      ["EDGE " + detail.team_a, signedPct(latest.edge_a), tone(latest.edge_a)],
      ["EDGE " + detail.team_b, signedPct(latest.edge_b), tone(latest.edge_b)],
      ["BEST SIDE", latest.best_side
        ? (latest.best_side === "A" ? detail.team_a : detail.team_b) : "NONE"],
      ["BOOK AT", stamp(latest.forecast_at)],
    ])
  );
}

function renderPaper(detail) {
  var host = document.getElementById("d-paper");
  host.textContent = "";
  var open = (detail.positions || []).filter(function (p) { return Number(p.shares) > 0; });
  var trades = detail.trades || [];

  if (!open.length && !trades.length) {
    var why = detail.prior_source && detail.prior_source !== "seed"
      ? "No position. The edge has not cleared the entry threshold."
      : "No position, and none is possible: without an AI prior the model sits at the neutral seed, and the paper engine stands down rather than trade on the absence of a view.";
    host.appendChild(el("div", "hint", why));
    return;
  }

  open.forEach(function (p) {
    host.appendChild(
      kv([
        ["SIDE", p.outcome === "A" ? detail.team_a : detail.team_b],
        ["SHARES", Number(p.shares).toFixed(3)],
        ["AVG COST", pct(p.avg_cost)],
        ["REALIZED", money(p.realized_pnl)],
      ])
    );
  });

  if (trades.length) {
    var table = el("table");
    var head = el("thead");
    var headRow = el("tr");
    ["TIME", "ACT", "SIDE", "SHARES", "PRICE", "REASON"].forEach(function (h) {
      headRow.appendChild(el("th", null, h));
    });
    head.appendChild(headRow);
    table.appendChild(head);
    var body = el("tbody");
    trades.forEach(function (t) {
      var row = el("tr");
      row.appendChild(el("td", null, clock(t.traded_at)));
      row.appendChild(el("td", t.action === "BUY" ? "buy" : "sell", t.action));
      row.appendChild(el("td", null, t.outcome === "A" ? detail.team_a : detail.team_b));
      row.appendChild(el("td", "num", Number(t.shares).toFixed(3)));
      row.appendChild(el("td", "num", pct(t.price)));
      row.appendChild(el("td", null, t.reason));
      body.appendChild(row);
    });
    table.appendChild(body);
    var scroller = el("div", "scroller");
    scroller.appendChild(table);
    host.appendChild(scroller);
  }
}

function renderObservations(detail) {
  var body = document.getElementById("d-obs");
  body.textContent = "";
  var rows = (detail.history || []).slice().reverse().slice(0, 120);
  setText(document.getElementById("d-obs-count"), "[" + (detail.history || []).length + "]");
  if (!rows.length) {
    var empty = el("tr");
    var cell = el("td", null, "No observations yet.");
    cell.colSpan = 9;
    empty.appendChild(cell);
    body.appendChild(empty);
    return;
  }
  rows.forEach(function (r) {
    var row = el("tr");
    row.appendChild(el("td", null, clock(r.forecast_at)));
    row.appendChild(el("td", "num", pct(r.probability_a)));
    row.appendChild(el("td", "num", pct(r.market_midpoint_a)));
    row.appendChild(el("td", "num " + tone(r.edge_a), signedPct(r.edge_a)));
    row.appendChild(el("td", "num " + tone(r.edge_b), signedPct(r.edge_b)));
    row.appendChild(el("td", null, num(r.maps_a, "0") + "–" + num(r.maps_b, "0")));
    row.appendChild(el("td", null, num(r.rounds_a, "0") + "–" + num(r.rounds_b, "0")));
    row.appendChild(el("td", null, r.current_map || "—"));
    row.appendChild(el("td", null, r.state_source || "—"));
    body.appendChild(row);
  });
}

/* Top level -------------------------------------------------------------- */

function render(detail) {
  var title = document.getElementById("d-title");
  title.textContent = "";
  title.appendChild(document.createTextNode(detail.team_a));
  title.appendChild(el("span", "vs", "vs"));
  title.appendChild(document.createTextNode(detail.team_b));

  setText(
    document.getElementById("d-comp"),
    [detail.league, detail.serie, detail.tournament].filter(Boolean).join(" · ") +
      " · BO" + num(detail.best_of)
  );

  var resolved = detail.status === "resolved";
  var dot = document.getElementById("d-dot");
  if (resolved) {
    setClass(dot, "dot stale");
    setText(
      document.getElementById("d-state"),
      "RESOLVED · " + (detail.winner === "A" ? detail.team_a : detail.team_b) + " WON"
    );
  } else if (detail.ended) {
    setClass(dot, "dot stale");
    setText(document.getElementById("d-state"), "FINISHED · AWAITING SETTLEMENT");
  } else if (detail.live) {
    setClass(dot, "dot");
    setText(document.getElementById("d-state"), "LIVE");
  } else {
    setClass(dot, "dot stale");
    setText(document.getElementById("d-state"), String(detail.status || "").toUpperCase());
  }
  setText(document.getElementById("d-sched"), "SCHEDULED " + stamp(detail.scheduled_at));

  var latest = detail.latest || {};
  setText(document.getElementById("d-model"), pct(latest.probability_a));
  setText(document.getElementById("d-market"), pct(latest.market_midpoint_a));

  var best = latest.edge_a === null || latest.edge_a === undefined
    ? null : Math.max(latest.edge_a, latest.edge_b);
  var edgeNode = document.getElementById("d-edge");
  setText(edgeNode, signedPct(best));
  setClass(edgeNode, tone(best));

  setText(document.getElementById("d-maps"), num(latest.maps_a, "0") + "–" + num(latest.maps_b, "0"));
  var hasRounds = (latest.rounds_a || 0) + (latest.rounds_b || 0) > 0;
  setText(document.getElementById("d-rounds"), hasRounds
    ? latest.rounds_a + "–" + latest.rounds_b : "NO FEED");
  setText(document.getElementById("d-liq"), money(detail.liquidity));

  setText(document.getElementById("d-points"), "[" + (detail.history || []).length + "]");
  drawChart(detail.history || []);
  renderAi(detail);
  renderFacts(detail);
  renderBook(detail);
  renderPaper(detail);
  renderObservations(detail);

  setText(document.getElementById("d-ids"), detail.match_id);
  document.title = detail.team_a + " vs " + detail.team_b + " // POLYTRADE";
}

function fail(message) {
  setClass(document.getElementById("d-dot"), "dot down");
  setText(document.getElementById("d-state"), message);
}

function refresh() {
  var id = matchIdFromPath();
  if (!id) {
    fail("NO MATCH IN URL");
    return Promise.resolve();
  }
  return fetch("/api/match?id=" + encodeURIComponent(id), { cache: "no-store" })
    .then(function (response) {
      if (response.status === 404) throw new Error("UNKNOWN MATCH");
      if (!response.ok) throw new Error("STATUS " + response.status);
      return response.json();
    })
    .then(render)
    .catch(function (error) {
      fail(String(error.message || error));
      if (window.console) console.error(error);
    });
}

refresh();
setInterval(refresh, REFRESH_MS);
