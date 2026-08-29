/* Dashboard client.

   Three rules, in priority order:

   1. Nothing moves on its own. Cards are created once and updated in place,
      and the sort key is the scheduled start time -- a value that does not
      change between polls. Ordering previously came from the newest forecast
      timestamp, which is rewritten on every tick, so the whole board
      reshuffled once a minute.
   2. Numbers travel rather than teleport. A probability going 0.22 -> 0.55 is
      interpolated over a few hundred milliseconds, so the eye can follow which
      figure moved and in which direction.
   3. Only real change is signalled. A card that took new numbers gets one
      short glow; a quiet poll paints nothing.

   DOM nodes are built rather than assembled from innerHTML strings: match and
   tournament names come from a third party and this page has no template
   escaping layer of its own. */

"use strict";

var REFRESH_MS = 5000;
var STALE_MS = 180000;
var UPCOMING_LIMIT = 48;
var PENDING_LIMIT = 12;
var COUNT_MS = 620;

/* Board filter.

   Most of the slate has no AI prior, and a card whose model reads "seed 50%"
   is an absence of a view rather than a view worth reading. Hiding those by
   default is what makes the board legible; the hidden count stays on screen so
   the filter cannot quietly shrink the picture. */
var FILTER_KEY = "polytrade.aiPricedOnly";
var aiPricedOnly = true;
var lastPayload = null;

function hasPrior(match) {
  return !!(match.prior_source && match.prior_source !== "seed");
}

function loadFilter() {
  try {
    var stored = window.localStorage.getItem(FILTER_KEY);
    if (stored !== null) aiPricedOnly = stored === "1";
  } catch (error) {
    // Private browsing and similar; the default stands.
  }
}

function saveFilter() {
  try {
    window.localStorage.setItem(FILTER_KEY, aiPricedOnly ? "1" : "0");
  } catch (error) {
    // Not being able to remember the choice is not worth failing over.
  }
}

var registry = {
  live: Object.create(null),
  pending: Object.create(null),
  soon: Object.create(null),
};
var tradeSignature = null;

/* Animated numbers ------------------------------------------------------- */

var motion = [];
var frameQueued = false;
var canAnimate =
  typeof requestAnimationFrame === "function" &&
  !(typeof matchMedia === "function" &&
    matchMedia("(prefers-reduced-motion: reduce)").matches);

function easeOut(t) {
  return 1 - Math.pow(1 - t, 3);
}

function step(now) {
  frameQueued = false;
  var live = [];
  for (var i = 0; i < motion.length; i += 1) {
    var m = motion[i];
    var progress = Math.min(1, (now - m.start) / m.duration);
    var value = m.from + (m.to - m.from) * easeOut(progress);
    m.node.textContent = m.format(progress === 1 ? m.to : value);
    if (progress < 1) live.push(m);
  }
  motion = live;
  if (motion.length) queueFrame();
}

function queueFrame() {
  if (frameQueued || !canAnimate) return;
  frameQueued = true;
  requestAnimationFrame(step);
}

/* Set a numeric field, travelling from its previous value.

   Returns true when the value actually changed, which is what decides whether
   the owning card is worth highlighting. */
function setNumber(node, value, format) {
  var target = value === null || value === undefined || isNaN(value) ? null : Number(value);
  var painted = node._painted === true;
  var previous = painted ? node._value : null;
  if (painted && previous === target) return false;
  node._value = target;
  node._painted = true;

  // Nothing to travel between when either end is absent, and a first paint is
  // not a change worth announcing.
  if (target === null || previous === null || !canAnimate) {
    node.textContent = format(target);
    return painted && previous !== target;
  }

  for (var i = 0; i < motion.length; i += 1) {
    if (motion[i].node === node) {
      motion.splice(i, 1);
      break;
    }
  }
  motion.push({
    node: node,
    from: previous,
    to: target,
    start: typeof performance === "object" ? performance.now() : Date.now(),
    duration: COUNT_MS,
    format: format,
  });
  queueFrame();
  return true;
}

/* Formatting ------------------------------------------------------------- */

function pct(value) {
  return value === null || value === undefined || isNaN(value)
    ? "—"
    : (Number(value) * 100).toFixed(1) + "%";
}

function signedPct(value) {
  if (value === null || value === undefined || isNaN(value)) return "—";
  var n = Number(value) * 100;
  return (n >= 0 ? "+" : "") + n.toFixed(1) + "%";
}

function money(value) {
  return value === null || value === undefined || isNaN(value)
    ? "—"
    : "$" + Number(value).toFixed(2);
}

function signedMoney(value) {
  if (value === null || value === undefined || isNaN(value)) return "—";
  var amount = Number(value);
  return (amount >= 0 ? "+" : "−") + "$" + Math.abs(amount).toFixed(2);
}

function whole(value) {
  return value === null || value === undefined || isNaN(value)
    ? "—"
    : String(Math.round(Number(value)));
}

function fixed3(value) {
  return value === null || value === undefined || isNaN(value)
    ? "—"
    : Number(value).toFixed(3);
}

function num(value, fallback) {
  return value === null || value === undefined ? (fallback || "—") : value;
}

function clock(iso) {
  if (!iso) return "—";
  var when = new Date(iso);
  return isNaN(when.getTime()) ? "—" : when.toLocaleTimeString([], { hour12: false });
}

function ageMs(iso) {
  if (!iso) return Infinity;
  var when = new Date(iso);
  return isNaN(when.getTime()) ? Infinity : Date.now() - when.getTime();
}

function secondsAgo(iso) {
  var age = ageMs(iso);
  if (!isFinite(age)) return "";
  var seconds = Math.max(0, Math.round(age / 1000));
  if (seconds < 90) return seconds + "s";
  var minutes = Math.round(seconds / 60);
  return minutes < 90 ? minutes + "m" : Math.round(minutes / 60) + "h";
}

function startsIn(iso) {
  if (!iso) return "TBD";
  var when = new Date(iso);
  if (isNaN(when.getTime())) return "TBD";
  var minutes = Math.round((when.getTime() - Date.now()) / 60000);
  if (minutes <= 0) return "SOON";
  if (minutes < 60) return "T-" + minutes + "M";
  var hours = Math.floor(minutes / 60);
  if (hours < 24) return "T-" + hours + "H";
  return "T-" + Math.floor(hours / 24) + "D";
}

function edgeTone(value) {
  if (value === null || value === undefined || isNaN(value)) return "flat";
  if (Number(value) > 0.02) return "up";
  if (Number(value) < -0.02) return "down";
  return "flat";
}

/* DOM helpers ------------------------------------------------------------ */

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function setText(node, value) {
  var next = String(value);
  if (node.textContent !== next) node.textContent = next;
}

function setClass(node, value) {
  if (node.className !== value) node.className = value;
}

/* Position a marker, or hide it when there is nothing to position.

   Defaulting an absent value to zero pinned the marker to the left edge, which
   reads as "the model says 0%" on every match that has not been ticked yet.
   Absence and zero are different claims and must not render the same. */
function place(node, ratio) {
  var known = ratio !== null && ratio !== undefined && !isNaN(ratio);
  var hidden = known ? "" : "none";
  if (node.style.display !== hidden) node.style.display = hidden;
  if (!known) return false;
  var next = (Math.max(0, Math.min(1, Number(ratio))) * 100).toFixed(2) + "%";
  if (node.style.left !== next) node.style.left = next;
  return true;
}

/* Card ------------------------------------------------------------------- */

function figure(refs, key, label) {
  var cell = el("div");
  cell.appendChild(el("span", null, label));
  refs[key] = el("b");
  cell.appendChild(refs[key]);
  return cell;
}

function buildCard(matchId) {
  var refs = {};
  // The card is a link so the whole surface is clickable and the URL is
  // shareable; detail now lives on its own page rather than crowding this one.
  var link = el("a", "match-link");
  link.href = "/match/" + encodeURIComponent(matchId);
  var card = el("article", "match enter");
  link.appendChild(card);
  card.addEventListener("animationend", function () {
    card.classList.remove("enter");
    card.classList.remove("moved");
  });

  var meta = el("div", "meta");
  var left = el("div", "comp");
  refs.tag = el("span", "tag");
  refs.comp = el("span");
  left.appendChild(refs.tag);
  left.appendChild(refs.comp);
  refs.age = el("div");
  meta.appendChild(left);
  meta.appendChild(refs.age);
  card.appendChild(meta);

  var board = el("div", "board");
  refs.teamA = el("div", "team");
  refs.maps = el("div", "maps");
  refs.teamB = el("div", "team b");
  board.appendChild(refs.teamA);
  board.appendChild(refs.maps);
  board.appendChild(refs.teamB);
  card.appendChild(board);

  var subline = el("div", "subline");
  refs.rounds = el("div", "rounds");
  refs.mapName = el("div");
  subline.appendChild(refs.rounds);
  subline.appendChild(refs.mapName);
  card.appendChild(subline);

  var track = el("div", "track");
  refs.track = track;
  refs.trackNote = el("div", "track-note");
  track.appendChild(refs.trackNote);
  refs.band = el("div", "band");
  refs.market = el("div", "mark market");
  refs.model = el("div", "mark model");
  refs.market.setAttribute("data-label", "MKT");
  refs.model.setAttribute("data-label", "AI");
  track.appendChild(refs.band);
  track.appendChild(refs.market);
  track.appendChild(refs.model);
  card.appendChild(track);

  var figures = el("div", "figures");
  figures.appendChild(figure(refs, "modelValue", "MODEL"));
  figures.appendChild(figure(refs, "marketValue", "MARKET"));
  figures.appendChild(figure(refs, "edgeValue", "BEST EDGE"));
  card.appendChild(figures);

  refs.badge = el("div", "prior-badge");
  card.appendChild(refs.badge);

  return { node: link, card: card, refs: refs };
}

function updateCard(entry, match) {
  var refs = entry.refs;
  var pending = !!match.ended && match.status === "open";
  var live = !!match.live && !match.ended && match.status === "open";

  var card = entry.card;
  var base = "match" + (live ? " is-live" : pending ? " is-pending" : "");
  if (card.classList.contains("enter")) base += " enter";
  if (card.classList.contains("moved")) base += " moved";
  setClass(card, base);

  setClass(refs.tag, live ? "tag" : pending ? "tag pending" : "tag wait");
  setText(refs.tag, live ? "LIVE" : pending ? "PENDING" : startsIn(match.scheduled_at));
  setText(refs.comp, [match.league, match.tournament].filter(Boolean).join(" · "));
  var age = secondsAgo(match.book_source_at);
  setText(refs.age, "BO" + num(match.best_of) + (age ? " · " + age : ""));

  setText(refs.teamA, match.team_a);
  setText(refs.teamB, match.team_b);
  setText(refs.maps, num(match.maps_a, "0") + "–" + num(match.maps_b, "0"));

  var hasRounds = (match.rounds_a || 0) + (match.rounds_b || 0) > 0;
  setClass(refs.rounds, hasRounds ? "rounds" : "rounds idle");
  setText(refs.rounds, hasRounds ? match.rounds_a + "–" + match.rounds_b + " RDS" : "NO ROUND FEED");
  setText(refs.mapName, String(match.current_map || "—").toUpperCase());

  var priced = match.prior_source && match.prior_source !== "seed";
  // Before the first tick there is no forecast, but a priced match still has
  // a view: at 0-0 with no round feed the engine returns the prior unchanged,
  // so the prior is the model's current number rather than a stand-in for it.
  var model = match.probability_a;
  if (model === null || model === undefined) {
    model = match.prior_probability_llm;
  }
  var market = match.market_midpoint_a;

  setClass(refs.model, "mark model" + (priced ? "" : " seed"));
  place(refs.model, model);
  place(refs.market, market);

  // The band spans model to market: its width is the disagreement, and its
  // colour says which way the model leans.
  var haveBoth =
    model !== null && model !== undefined && !isNaN(model) &&
    market !== null && market !== undefined && !isNaN(market);
  if (haveBoth) {
    var lo = Math.min(model, market);
    var hi = Math.max(model, market);
    refs.band.style.left = (lo * 100).toFixed(2) + "%";
    refs.band.style.width = ((hi - lo) * 100).toFixed(2) + "%";
    setClass(refs.band, "band" + (model >= market ? "" : " negative"));
  } else {
    refs.band.style.width = "0%";
  }
  // An empty track should look inert rather than look like a reading of zero.
  setClass(refs.track, "track" + (model === null || model === undefined ? " idle" : ""));
  setText(refs.trackNote, model === null || model === undefined ? "AWAITING FIRST TICK" : "");

  var bestEdge = null;
  if (match.edge_a !== null && match.edge_a !== undefined) {
    bestEdge = Math.max(match.edge_a, match.edge_b);
  }

  var changed = false;
  changed = setNumber(refs.modelValue, model, pct) || changed;
  changed = setNumber(refs.marketValue, market, pct) || changed;
  changed = setNumber(refs.edgeValue, bestEdge, signedPct) || changed;
  setClass(refs.edgeValue, edgeTone(bestEdge));

  // Grounding is the signal that matters now: a prior written with fetched
  // team facts is a different thing from one written without, and the board
  // should not let them look alike.
  var hasPrior = match.prior_source && match.prior_source !== "seed";
  var grounded = (match.prior_grounded_teams || 0) > 0;
  setClass(
    refs.badge,
    "prior-badge" + (grounded ? " grounded" : hasPrior ? " priced" : "")
  );
  setText(
    refs.badge,
    hasPrior
      ? (grounded ? "GROUNDED" : "AI PRIOR") +
        " · " + String(match.prior_confidence || "").toUpperCase() +
        " · " + String(match.prior_backend || "").toUpperCase() + " →"
      : "NO AI PRIOR · SEED 50% →"
  );

  return changed;
}

/* Keyed, stably ordered list -------------------------------------------- */

function sortKey(match) {
  // Scheduled start is fixed for the life of a match, so the order it produces
  // is the same on every poll. Anything derived from the latest forecast is
  // rewritten each tick and makes the board reshuffle under the reader.
  return String(match.scheduled_at || "") + "|" + String(match.match_id || "");
}

function syncList(node, store, matches, emptyText) {
  var ordered = matches.slice().sort(function (a, b) {
    var ka = sortKey(a);
    var kb = sortKey(b);
    return ka < kb ? -1 : ka > kb ? 1 : 0;
  });

  var seen = Object.create(null);
  var placeholder = node.querySelector(".empty");

  ordered.forEach(function (match, index) {
    var key = match.match_id;
    seen[key] = true;
    var entry = store[key];
    var isNew = !entry;
    if (isNew) {
      entry = buildCard(key);
      entry.card.style.animationDelay = Math.min(index, 12) * 22 + "ms";
      store[key] = entry;
    }
    var changed = updateCard(entry, match);
    if (changed && !isNew) {
      entry.card.classList.remove("moved");
      void entry.card.offsetWidth;
      entry.card.classList.add("moved");
    }
  });

  Object.keys(store).forEach(function (key) {
    if (!seen[key]) {
      if (store[key].node.parentNode === node) node.removeChild(store[key].node);
      delete store[key];
    }
  });

  if (!ordered.length) {
    if (!placeholder) {
      node.textContent = "";
      node.appendChild(el("div", "empty", emptyText));
    }
    return;
  }
  if (placeholder) node.removeChild(placeholder);

  ordered.forEach(function (match, index) {
    var entry = store[match.match_id];
    if (entry && node.children[index] !== entry.node) {
      node.insertBefore(entry.node, node.children[index] || null);
    }
  });
}

function syncTrades(node, trades) {
  var signature = trades.length
    ? trades.length + ":" + (trades[0].trade_id || trades[0].traded_at)
    : "0";
  if (signature === tradeSignature) return;
  tradeSignature = signature;

  node.textContent = "";
  if (!trades.length) {
    var row = el("tr");
    var cell = el("td", null, "No paper trades yet.");
    cell.colSpan = 10;
    row.appendChild(cell);
    node.appendChild(row);
    return;
  }
  trades.forEach(function (trade) {
    var line = el("tr");
    line.appendChild(el("td", null, clock(trade.traded_at)));
    line.appendChild(el("td", null, trade.team_a + " – " + trade.team_b));
    line.appendChild(el("td", null, String(trade.entry_strategy || trade.decision_strategy || "—").toUpperCase()));
    line.appendChild(el("td", trade.action === "BUY" ? "buy" : "sell", trade.action));
    line.appendChild(el("td", null, trade.outcome));
    line.appendChild(el("td", "num", Number(trade.shares).toFixed(3)));
    line.appendChild(el("td", "num", pct(trade.price)));
    line.appendChild(el("td", "num " + (Number(trade.slippage || 0) > 0 ? "down" : "flat"), signedPct(trade.slippage || 0)));
    line.appendChild(el("td", "num", money(trade.fee || 0)));
    line.appendChild(el("td", null, trade.reason));
    node.appendChild(line);
  });
}

function renderStrategies(strategies) {
  var byName = Object.create(null);
  (strategies || []).forEach(function (item) { byName[item.strategy] = item; });
  [
    ["pre-match", "pre"],
    ["map-boundary", "map"],
    ["round-live", "round"],
    ["maps-only-degraded", "degraded"],
  ].forEach(function (pair) {
    var item = byName[pair[0]] || {};
    var pnl = Number(item.total_pnl || 0);
    var paperEnabled = item.paper_enabled === undefined
      ? Number(item.decisions || 0)
      : Number(item.paper_enabled || 0);
    var forecasts = item.forecasts === undefined
      ? paperEnabled
      : Number(item.forecasts || 0);
    var pnlNode = document.getElementById("strategy-" + pair[1] + "-pnl");
    setNumber(pnlNode, pnl, signedMoney);
    setClass(pnlNode, pnl > 0 ? "up" : pnl < 0 ? "down" : "");
    setText(
      document.getElementById("strategy-" + pair[1] + "-meta"),
      "FORECAST " + whole(forecasts) + " · PAPER " + whole(paperEnabled) +
        " · ENTRY " + whole(item.entry_enabled || 0)
    );
    setText(
      document.getElementById("strategy-" + pair[1] + "-open"),
      "SIGNAL " + whole(item.signals || 0) + " · ORDER " + whole(item.orders || 0) +
        " · FILL " + whole(item.fills || 0) + " · TRADE " + whole(item.trades || 0) +
        " · REALIZED " + signedMoney(item.realized_pnl || 0) +
        " · OPEN " + whole(item.open_positions || 0)
    );
  });
}

function renderFeedHealth(data) {
  var run = data.collector || {};
  var feed = run.feed || {};
  var enabled = feed.enabled !== false;
  var connected = !!feed.connected;
  var stale = !!feed.stale;
  var statusNode = document.getElementById("feed-status");
  setText(statusNode, !enabled ? "DISABLED" : connected ? (stale ? "STALE" : "CONNECTED") : "DISCONNECTED");
  setClass(statusNode, !enabled ? "warn" : !connected ? "bad" : stale ? "warn" : "good");
  setText(
    document.getElementById("feed-age"),
    feed.last_message_age_seconds === null || feed.last_message_age_seconds === undefined
      ? "—"
      : Math.round(Number(feed.last_message_age_seconds)) + "s"
  );
  setText(
    document.getElementById("feed-coverage"),
    feed.round_coverage === null || feed.round_coverage === undefined
      ? "IDLE"
      : pct(feed.round_coverage) + " · " + whole(feed.round_level || 0) + "/" + whole(feed.tracked_live || 0)
  );
  setText(document.getElementById("feed-placeholders"), whole(feed.placeholder_count || 0));
  setText(document.getElementById("feed-frozen"), whole(feed.frozen_states || 0));
  var rejected = (data.state_guard || {}).total;
  setText(
    document.getElementById("feed-rejected"),
    whole(rejected || 0) + " TOTAL · " + whole(feed.rejected_transitions || 0) + " CYCLE"
  );
}

function renderExecution(data) {
  var execution = data.execution || {};
  var worker = execution.worker || {};
  var heartbeatAge = ageMs(worker.last_heartbeat_at);
  var statusNode = document.getElementById("exec-status");
  var status;
  var tone;
  if (execution.kill_switch) {
    status = "KILL SWITCH";
    tone = "bad";
  } else if (!worker.last_heartbeat_at) {
    status = "STARTING";
    tone = "warn";
  } else if (heartbeatAge > 15000) {
    status = "STALE · " + secondsAgo(worker.last_heartbeat_at);
    tone = "bad";
  } else if (worker.status === "degraded") {
    status = "DEGRADED";
    tone = "warn";
  } else {
    status = "RUNNING · " + secondsAgo(worker.last_heartbeat_at);
    tone = "good";
  }
  setText(statusNode, status);
  setClass(statusNode, tone);
  setText(
    document.getElementById("exec-orders"),
    whole(execution.orders || 0) + " TOTAL · O" +
      whole(execution.pending || 0) + " · R" +
      whole(execution.rejected_orders || 0)
  );
  setText(
    document.getElementById("exec-fill-rate"),
    execution.fill_rate === null || execution.fill_rate === undefined
      ? "AWAITING FILLS"
      : pct(execution.fill_rate) + " · F" + whole(execution.filled_orders || 0) +
        " / P" + whole(execution.partial_orders || 0)
  );
  var slippage = document.getElementById("exec-slippage");
  setText(slippage, signedPct(execution.avg_slippage));
  setClass(slippage, Number(execution.avg_slippage || 0) > 0 ? "bad" : "good");
  setText(
    document.getElementById("exec-latency"),
    execution.avg_latency_ms === null || execution.avg_latency_ms === undefined
      ? "—" : Math.round(Number(execution.avg_latency_ms)) + "ms"
  );
  var risk = document.getElementById("exec-risk");
  setText(
    risk,
    money(execution.fees || 0) + " · " +
      (execution.kill_switch ? String(execution.kill_switch_reason || "BLOCKED").toUpperCase() : "LIMITS ON")
  );
  setClass(risk, execution.kill_switch ? "bad" : "good");
}

/* Scoreboard ------------------------------------------------------------- */

function renderScoreboard(report) {
  report = report || {};
  var ai = report.ai || {};
  var market = report.market || {};
  var n = ai.n || 0;

  setText(document.getElementById("c-scored"), "[" + n + "]");
  setNumber(document.getElementById("ai-brier"), ai.brier, fixed3);
  setNumber(document.getElementById("mkt-brier"), market.brier, fixed3);

  setText(
    document.getElementById("ai-extra"),
    n ? "LOG LOSS " + fixed3(ai.log_loss) + " · HIT " + pct(ai.accuracy) + " · n=" + n
      : "no scored matches yet"
  );
  setText(
    document.getElementById("mkt-extra"),
    market.n ? "LOG LOSS " + fixed3(market.log_loss) + " · HIT " + pct(market.accuracy)
      : "no scored matches yet"
  );
  setText(
    document.getElementById("coin-note"),
    n && ai.brier !== null && ai.brier !== undefined
      ? (report.ai_beats_coin_flip ? "AI beats it" : "AI does not beat it")
      : "the bar to clear"
  );

  var verdict = document.getElementById("verdict");
  document.getElementById("col-ai").className = "scorecol";
  document.getElementById("col-mkt").className = "scorecol";

  if (!n) {
    setClass(verdict, "verdict idle");
    setText(verdict, "AWAITING THE FIRST RESOLVED AI-PRICED MATCH");
  } else {
    var edge = report.brier_edge;
    var ahead = edge > 0;
    setClass(verdict, "verdict " + (ahead ? "ahead" : "behind"));
    setText(
      verdict,
      (ahead ? "AI AHEAD OF THE MARKET" : "MARKET AHEAD OF THE AI") +
        " BY " + fixed3(Math.abs(edge)) + " BRIER"
    );
    document.getElementById(ahead ? "col-ai" : "col-mkt").className = "scorecol winner";
  }

  var parts = [];
  if (n && !report.reliable) {
    parts.push("n=" + n + " is far too small to mean anything; 30+ resolved matches before this is worth reading.");
  }
  if (report.resolved_total) {
    parts.push(report.resolved_total + " resolved matches carry an AI prior.");
  }
  if (report.missing_baseline) {
    parts.push(report.missing_baseline + " skipped: no market price recorded at prior time, and inventing one would fake the comparison.");
  }
  if (report.excluded) {
    parts.push(
      report.excluded +
        " earlier priors are excluded: written with no web access and no fetched facts, they tracked the market's own summary rather than forming a view."
    );
  }
  if (!parts.length) {
    parts.push("Scores appear once an AI-priced match finishes and Polymarket settles it, typically 6-24h after the match.");
  }
  var caveat = document.getElementById("caveat");
  setClass(caveat, "caveat" + (n && !report.reliable ? " warn" : ""));
  setText(caveat, parts.join(" "));
}

/* Top level -------------------------------------------------------------- */

function setBeacon(state, label) {
  setClass(document.getElementById("live-dot"), "dot" + (state ? " " + state : ""));
  setText(document.getElementById("live-label"), label);
}

function render(data) {
  lastPayload = data;
  var counts = data.counts || {};
  var account = data.account || {};
  var matches = data.matches || [];

  setNumber(document.getElementById("s-live"), counts.live, whole);
  setNumber(document.getElementById("s-pending"), counts.pending, whole);
  setNumber(document.getElementById("s-tracked"), counts.matches, whole);
  setNumber(document.getElementById("s-priced"), counts.grounded_priors, whole);
  setNumber(document.getElementById("s-resolved"), counts.resolved, whole);
  setNumber(document.getElementById("s-equity"), account.equity, money);

  var returnNode = document.getElementById("s-return");
  setNumber(returnNode, account.return, signedPct);
  setClass(returnNode, account.return >= 0 ? "up" : "down");

  var run = data.collector;
  renderFeedHealth(data);
  renderExecution(data);
  setText(
    document.getElementById("cycle"),
    "SYNC " + clock(data.generated_at) +
      (run ? " · CYCLE " + clock(run.finished_at || run.started_at) + " · " + run.ticked + " TICKED" : "")
  );

  var freshness = ageMs(data.latest_forecast_at);
  var feed = run && run.feed;
  var mapsOnly = run && (run.notices || []).some(function (notice) {
    return String(notice).toLowerCase().indexOf("maps-only") >= 0;
  });
  if (!data.latest_forecast_at) setBeacon("stale", "AWAITING DATA");
  else if (freshness > STALE_MS) setBeacon("stale", "STALE FEED");
  else if (feed && counts.live && !feed.connected) setBeacon("down", "SPORTS WS DOWN");
  else if (feed && counts.live && feed.stale) setBeacon("stale", "ROUND FEED STALE");
  else if (
    feed && counts.live && feed.round_coverage !== null &&
    feed.round_coverage !== undefined && feed.round_coverage < 1
  ) setBeacon("stale", "PARTIAL ROUND FEED");
  else if (mapsOnly) setBeacon("stale", "MAPS-ONLY FEED");
  else setBeacon(null, "LIVE FEED");

  var live = matches.filter(function (m) {
    return m.live && !m.ended && m.status === "open";
  });
  var pendingAll = matches.filter(function (m) { return m.ended && m.status === "open"; });
  var pending = pendingAll
    .sort(function (a, b) {
      return String(b.finished_observed_at || b.scheduled_at || "").localeCompare(
        String(a.finished_observed_at || a.scheduled_at || "")
      );
    })
    .slice(0, PENDING_LIMIT);
  var soon = matches
    .filter(function (m) { return !m.live && !m.ended && m.status === "open"; })
    .slice(0, UPCOMING_LIMIT);

  var totals = { live: live.length, pending: pending.length, soon: soon.length };
  if (aiPricedOnly) {
    live = live.filter(hasPrior);
    pending = pending.filter(hasPrior);
    soon = soon.filter(hasPrior);
  }
  var hidden =
    (totals.live - live.length) +
    (totals.soon - soon.length) +
    (totals.pending - pending.length);
  setText(
    document.getElementById("filter-note"),
    aiPricedOnly
      ? (hidden ? hidden + " matches hidden — no AI prior, model held at seed 50%"
                : "nothing hidden — every tracked match has a prior")
      : "showing every tracked match, priced or not"
  );

  setText(
    document.getElementById("c-live"),
    "[" + live.length + (live.length < totals.live ? "/" + totals.live : "") + "]"
  );
  setText(
    document.getElementById("c-pending"),
    "[" + pending.length + (pending.length < pendingAll.length ? "/" + pendingAll.length : "") + "]"
  );
  setText(
    document.getElementById("c-soon"),
    "[" + soon.length + (soon.length < totals.soon ? "/" + totals.soon : "") + "]"
  );

  syncList(
    document.getElementById("live-list"),
    registry.live,
    live,
    aiPricedOnly && totals.live
      ? "No live match has an AI prior. Turn the filter off to see the rest."
      : "No CS2 match is live right now."
  );
  // The filter may empty this section, but the backlog is operational state,
  // not an opinion. Saying "none are awaiting settlement" while two dozen are
  // stuck would hide a stalled settlement pipeline behind an analysis filter,
  // so the count is stated in the placeholder either way.
  var pendingHidden = pendingAll.length - pending.length;
  syncList(
    document.getElementById("pending-list"),
    registry.pending,
    pending,
    pendingHidden
      ? pendingAll.length +
        " finished matches are waiting for Polymarket to settle; none has an AI prior. Turn the filter off to see them."
      : "No finished matches are awaiting settlement."
  );
  syncList(
    document.getElementById("soon-list"),
    registry.soon,
    soon,
    aiPricedOnly && totals.soon
      ? "No upcoming match has an AI prior yet. Turn the filter off to see the rest."
      : "No upcoming matches tracked."
  );
  renderScoreboard(data.scoring);
  renderStrategies(account.strategies || []);
  syncTrades(document.getElementById("trades"), account.trades || []);
}

function refresh() {
  return fetch("/api/status", { cache: "no-store" })
    .then(function (response) {
      if (!response.ok) throw new Error("status " + response.status);
      return response.json();
    })
    .then(render)
    .catch(function (error) {
      setBeacon("down", "CONNECTION LOST");
      if (window.console) console.error(error);
    });
}

function applyFilterButton() {
  var button = document.getElementById("filter-toggle");
  button.setAttribute("aria-pressed", aiPricedOnly ? "true" : "false");
  setText(
    document.getElementById("filter-label"),
    aiPricedOnly ? "AI-PRICED ONLY" : "SHOWING ALL"
  );
}

loadFilter();
applyFilterButton();
document.getElementById("filter-toggle").addEventListener("click", function () {
  aiPricedOnly = !aiPricedOnly;
  saveFilter();
  applyFilterButton();
  // Re-render from the payload already in hand rather than making the reader
  // wait out the poll interval.
  if (lastPayload) render(lastPayload);
});

refresh();
setInterval(refresh, REFRESH_MS);
