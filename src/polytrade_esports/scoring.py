"""Score the AI prior against the market on the same resolved matches.

Two numbers decide whether any of this is worth running:

- **Brier score** — mean squared error of the probability. Lower is better.
  0.25 is what you get by always saying 50/50, so anything at or above that is
  worse than refusing to predict.
- **Log loss** — punishes confident mistakes far harder than Brier does. A
  model can win on Brier and lose badly here if it is occasionally sure and
  wrong.

Both are computed over the *same* matches, from probabilities recorded at the
*same* instant, because a comparison against a price sampled at some other
moment measures timing, not skill.
"""

import math
from typing import Any, Dict, List, Optional

# Log loss is unbounded at 0 and 1; clip so a single certain miss cannot swamp
# the average with infinity.
EPSILON = 1e-6
COIN_FLIP_BRIER = 0.25


def _clip(probability: float) -> float:
    return min(1.0 - EPSILON, max(EPSILON, float(probability)))


def brier(probability: float, outcome: int) -> float:
    return (float(probability) - float(outcome)) ** 2


def log_loss(probability: float, outcome: int) -> float:
    p = _clip(probability)
    return -(outcome * math.log(p) + (1 - outcome) * math.log(1.0 - p))


def _summarize(name: str, samples: List[Dict[str, float]]) -> Dict[str, Any]:
    if not samples:
        return {
            "name": name,
            "n": 0,
            "brier": None,
            "log_loss": None,
            "accuracy": None,
            "mean_probability": None,
        }
    n = len(samples)
    hits = sum(
        1
        for s in samples
        if (s["probability"] >= 0.5 and s["outcome"] == 1)
        or (s["probability"] < 0.5 and s["outcome"] == 0)
    )
    return {
        "name": name,
        "n": n,
        "brier": sum(brier(s["probability"], s["outcome"]) for s in samples) / n,
        "log_loss": sum(log_loss(s["probability"], s["outcome"]) for s in samples) / n,
        "accuracy": hits / n,
        "mean_probability": sum(s["probability"] for s in samples) / n,
    }


def score(database: Any) -> Dict[str, Any]:
    """Compare the AI prior with the market baseline on resolved matches."""
    rows = database.scoring_rows()

    ai_samples: List[Dict[str, float]] = []
    market_samples: List[Dict[str, float]] = []
    paired: List[Dict[str, Any]] = []
    missing_baseline = 0

    for row in rows:
        outcome = 1 if row["winner"] == "A" else 0
        ai_probability = row.get("ai_probability_a")
        market_probability = row.get("market_probability_a")
        if ai_probability is None:
            continue
        if market_probability is None:
            # Score nothing rather than compare against an invented price.
            missing_baseline += 1
            continue
        ai_samples.append({"probability": float(ai_probability), "outcome": outcome})
        market_samples.append(
            {"probability": float(market_probability), "outcome": outcome}
        )
        paired.append(
            {
                "match_id": row["match_id"],
                "team_a": row["team_a"],
                "team_b": row["team_b"],
                "winner": row["winner"],
                "ai_probability_a": float(ai_probability),
                "market_probability_a": float(market_probability),
                "ai_brier": brier(ai_probability, outcome),
                "market_brier": brier(market_probability, outcome),
                "resolved_at": row.get("resolved_at"),
                "confidence": row.get("confidence"),
                "model": row.get("model"),
            }
        )

    ai = _summarize("ai_prior", ai_samples)
    market = _summarize("market_baseline", market_samples)

    verdict = "insufficient data"
    if ai["n"] >= 1 and ai["brier"] is not None and market["brier"] is not None:
        if ai["brier"] < market["brier"]:
            verdict = "AI ahead of the market"
        elif ai["brier"] > market["brier"]:
            verdict = "market ahead of the AI"
        else:
            verdict = "level"

    return {
        "ai": ai,
        "market": market,
        "coin_flip_brier": COIN_FLIP_BRIER,
        "ai_beats_coin_flip": (
            None if ai["brier"] is None else ai["brier"] < COIN_FLIP_BRIER
        ),
        "brier_edge": (
            None
            if ai["brier"] is None or market["brier"] is None
            else market["brier"] - ai["brier"]
        ),
        "verdict": verdict,
        "resolved_total": len(rows),
        "missing_baseline": missing_baseline,
        "matches": paired[:100],
        # Small samples say nothing. Stated here so a flattering early number
        # is not mistaken for a result.
        "reliable": ai["n"] >= 30,
    }
