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

function place(node, ratio) {
  var value = ratio === null || ratio === undefined || isNaN(ratio) ? 0 : Number(ratio);
  var next = (Math.max(0, Math.min(1, value)) * 100).toFixed(2) + "%";
  if (node.style.left !== next) node.style.left = next;
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
  var model = match.probability_a;
  var market = match.market_midpoint_a;

  setClass(refs.model, "mark model" + (priced ? "" : " seed"));
  place(refs.model, model);
  place(refs.market, market);

  // The band spans model to market: its width is the disagreement, and its
  // colour says which way the model leans.
  if (model !== null && model !== undefined && market !== null && market !== undefined) {
    var lo = Math.min(model, market);
    var hi = Math.max(model, market);
    refs.band.style.left = (lo * 100).toFixed(2) + "%";
    refs.band.style.width = ((hi - lo) * 100).toFixed(2) + "%";
    setClass(refs.band, "band" + (model >= market ? "" : " negative"));
  } else {
    refs.band.style.width = "0%";
  }

  var bestEdge = null;
  if (match.edge_a !== null && match.edge_a !== undefined) {
    bestEdge = Math.max(match.edge_a, match.edge_b);
  }

  var changed = false;
  changed = setNumber(refs.modelValue, model, pct) || changed;
  changed = setNumber(refs.marketValue, market, pct) || changed;
  changed = setNumber(refs.edgeValue, bestEdge, signedPct) || changed;
  setClass(refs.edgeValue, edgeTone(bestEdge));

  var priced2 = match.prior_source && match.prior_source !== "seed";
  setClass(refs.badge, "prior-badge" + (priced2 ? " priced" : ""));
  setText(
    refs.badge,
    priced2
      ? "AI PRIOR · " + String(match.prior_confidence || "").toUpperCase() + " · DETAIL →"
      : "NO AI PRIOR · DETAIL →"
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
    cell.colSpan = 7;
    row.appendChild(cell);
    node.appendChild(row);
    return;
  }
  trades.forEach(function (trade) {
    var line = el("tr");
    line.appendChild(el("td", null, clock(trade.traded_at)));
    line.appendChild(el("td", null, trade.team_a + " – " + trade.team_b));
    line.appendChild(el("td", trade.action === "BUY" ? "buy" : "sell", trade.action));
    line.appendChild(el("td", null, trade.outcome));
    line.appendChild(el("td", "num", Number(trade.shares).toFixed(3)));
    line.appendChild(el("td", "num", pct(trade.price)));
    line.appendChild(el("td", null, trade.reason));
    node.appendChild(line);
  });
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
  var counts = data.counts || {};
  var account = data.account || {};
  var matches = data.matches || [];

  setNumber(document.getElementById("s-live"), counts.live, whole);
  setNumber(document.getElementById("s-pending"), counts.pending, whole);
  setNumber(document.getElementById("s-tracked"), counts.matches, whole);
  setNumber(document.getElementById("s-priced"), counts.priced, whole);
  setNumber(document.getElementById("s-resolved"), counts.resolved, whole);
  setNumber(document.getElementById("s-equity"), account.equity, money);

  var returnNode = document.getElementById("s-return");
  setNumber(returnNode, account.return, signedPct);
  setClass(returnNode, account.return >= 0 ? "up" : "down");

  var run = data.collector;
  setText(
    document.getElementById("cycle"),
    "SYNC " + clock(data.generated_at) +
      (run ? " · CYCLE " + clock(run.finished_at || run.started_at) + " · " + run.ticked + " TICKED" : "")
  );

  var freshness = ageMs(data.latest_forecast_at);
  var mapsOnly = run && (run.notices || []).some(function (notice) {
    return String(notice).toLowerCase().indexOf("maps-only") >= 0;
  });
  if (!data.latest_forecast_at) setBeacon("stale", "AWAITING DATA");
  else if (freshness > STALE_MS) setBeacon("stale", "STALE FEED");
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

  setText(document.getElementById("c-live"), "[" + live.length + "]");
  setText(
    document.getElementById("c-pending"),
    "[" + pending.length + (pending.length < pendingAll.length ? "/" + pendingAll.length : "") + "]"
  );
  setText(document.getElementById("c-soon"), "[" + soon.length + "]");

  syncList(document.getElementById("live-list"), registry.live, live, "No CS2 match is live right now.");
  syncList(
    document.getElementById("pending-list"),
    registry.pending,
    pending,
    "No finished matches are awaiting settlement."
  );
  syncList(document.getElementById("soon-list"), registry.soon, soon, "No upcoming matches tracked.");
  renderScoreboard(data.scoring);
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

refresh();
setInterval(refresh, REFRESH_MS);
