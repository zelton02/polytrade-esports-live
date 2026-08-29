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

Which of the two is *lower* is not by itself a result. Match outcomes are
noisy, so on a small sample one side leads by chance alone. The verdict below
therefore reports a direction only when a paired confidence interval on the
per-match Brier difference excludes zero; otherwise it says so, and estimates
how many resolved matches the observed effect would need.
"""

import math
from typing import Any, Dict, List, Optional

# Log loss is unbounded at 0 and 1; clip so a single certain miss cannot swamp
# the average with infinity.
EPSILON = 1e-6
COIN_FLIP_BRIER = 0.25

# Below this a paired interval is arithmetic, not evidence: two matches whose
# Brier differences happen to be identical give a zero-width interval and would
# otherwise be reported as a certainty.
MIN_VERDICT_SAMPLES = 10

# The production prior only prices books this deep, so only this stratum can
# decide whether the panel should replace it. Thinner books are scored, but
# separately: a thin book's price is a weaker opponent, which flatters any
# forecaster compared against it.
TRADEABLE_LIQUIDITY = 20000.0

# Two-sided 95% critical values for Student's t, by degrees of freedom. Past 30
# the normal approximation is within 2%, far finer than the decision this gates.
_T95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056,
    27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}


def _t_critical(degrees_of_freedom: int) -> float:
    if degrees_of_freedom <= 0:
        return float("inf")
    return _T95.get(degrees_of_freedom, 1.96)


def paired_comparison(differences: List[float]) -> Dict[str, Any]:
    """Test whether a per-match Brier difference is distinguishable from zero.

    ``differences`` are ai_brier - market_brier, so a negative mean favours the
    AI. Returns the interval, whether it excludes zero, and the sample size the
    observed effect would need to become significant.
    """
    n = len(differences)
    if n < 2:
        return {
            "n": n, "mean_difference": differences[0] if n else None,
            "standard_error": None, "t_statistic": None,
            "ci_low": None, "ci_high": None,
            "significant": False, "matches_needed": None,
        }
    mean = sum(differences) / n
    variance = sum((value - mean) ** 2 for value in differences) / (n - 1)
    deviation = math.sqrt(variance)
    error = deviation / math.sqrt(n)
    critical = _t_critical(n - 1)
    low, high = mean - critical * error, mean + critical * error
    significant = n >= MIN_VERDICT_SAMPLES and not (low <= 0.0 <= high)
    needed = None
    if mean and deviation:
        needed = int(math.ceil((critical * deviation / abs(mean)) ** 2))
        needed = max(needed, MIN_VERDICT_SAMPLES)
    return {
        "n": n,
        "mean_difference": mean,
        "standard_error": error,
        "t_statistic": (mean / error) if error else None,
        "ci_low": low,
        "ci_high": high,
        "significant": significant,
        "matches_needed": needed,
    }


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

    comparison = paired_comparison(
        [row["ai_brier"] - row["market_brier"] for row in paired]
    )

    # A lower Brier is a lead only if the paired interval says the lead is not
    # noise. Otherwise name the uncertainty rather than picking a winner.
    verdict = "insufficient data"
    if comparison["significant"]:
        verdict = (
            "AI ahead of the market"
            if comparison["mean_difference"] < 0
            else "market ahead of the AI"
        )
    elif ai["n"] >= 1:
        verdict = "too close to call"

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
        "comparison": comparison,
        "resolved_total": len(rows),
        "missing_baseline": missing_baseline,
        "matches": paired[:100],
        # Reliability is the interval's job, not a fixed row count: a large
        # effect settles early, a small one needs far more than 30 matches.
        "reliable": comparison["significant"],
    }


def shadow_score(database: Any) -> Dict[str, Any]:
    """Score each shadow panel cohort against the price it was blind to.

    Separate cohorts per model, because the candidate queue is keyed on the
    model: two models see the same fixtures and are therefore directly paired.
    Within a cohort the median is also compared against each single member, so
    the cost of running four can be checked against running one.
    """
    runs = database.shadow_scoring_rows()
    members: Dict[int, List[Dict[str, Any]]] = {}
    for row in database.shadow_member_rows():
        members.setdefault(int(row["run_id"]), []).append(row)

    cohorts: Dict[str, Dict[str, Any]] = {}
    for row in runs:
        key = "%s|%s" % (row["model"], row["panel_version"])
        cohort = cohorts.setdefault(
            key,
            {
                "model": row["model"],
                "panel_version": row["panel_version"],
                "panel_samples": [],
                "market_samples": [],
                "differences": [],
                "role_briers": {},
                "role_differences": {},
                "strata": {},
            },
        )
        outcome = 1 if row["winner"] == "A" else 0
        panel_brier = brier(row["consensus_probability_a"], outcome)
        market_brier = brier(row["market_probability_a"], outcome)
        cohort["panel_samples"].append(
            {"probability": float(row["consensus_probability_a"]), "outcome": outcome}
        )
        cohort["market_samples"].append(
            {"probability": float(row["market_probability_a"]), "outcome": outcome}
        )
        cohort["differences"].append(panel_brier - market_brier)

        liquidity = row.get("liquidity")
        band = (
            "unknown" if liquidity is None
            else "tradeable" if float(liquidity) >= TRADEABLE_LIQUIDITY
            else "thin"
        )
        stratum = cohort["strata"].setdefault(
            band, {"band": band, "n": 0, "panel_brier": 0.0, "market_brier": 0.0}
        )
        stratum["n"] += 1
        stratum["panel_brier"] += panel_brier
        stratum["market_brier"] += market_brier

        for member in members.get(int(row["run_id"]), []):
            role = member["role"]
            member_brier = brier(member["probability_a"], outcome)
            cohort["role_briers"].setdefault(role, []).append(member_brier)
            # Paired against the median on the SAME match, so the question is
            # whether the extra three members bought anything.
            cohort["role_differences"].setdefault(role, []).append(
                member_brier - panel_brier
            )

    reports = []
    for cohort in cohorts.values():
        panel = _summarize("shadow_panel", cohort["panel_samples"])
        market = _summarize("market_baseline", cohort["market_samples"])
        comparison = paired_comparison(cohort["differences"])
        roles = []
        for role in sorted(cohort["role_briers"]):
            values = cohort["role_briers"][role]
            roles.append(
                {
                    "role": role,
                    "n": len(values),
                    "brier": sum(values) / len(values),
                    "vs_panel": paired_comparison(cohort["role_differences"][role]),
                }
            )
        strata = []
        for band in sorted(cohort["strata"]):
            entry = cohort["strata"][band]
            strata.append(
                {
                    "band": entry["band"],
                    "n": entry["n"],
                    "panel_brier": entry["panel_brier"] / entry["n"],
                    "market_brier": entry["market_brier"] / entry["n"],
                }
            )
        verdict = "insufficient data"
        if comparison["significant"]:
            verdict = (
                "panel ahead of the market"
                if comparison["mean_difference"] < 0
                else "market ahead of the panel"
            )
        elif panel["n"] >= 1:
            verdict = "too close to call"
        reports.append(
            {
                "model": cohort["model"],
                "panel_version": cohort["panel_version"],
                "panel": panel,
                "market": market,
                "comparison": comparison,
                "verdict": verdict,
                "roles": roles,
                "strata": strata,
            }
        )

    reports.sort(key=lambda item: (-item["panel"]["n"], item["model"]))
    counts = database.shadow_run_counts()
    return {
        "cohorts": reports,
        "runs_total": counts["total"],
        "runs_scored": counts["scored"],
        "runs_awaiting_result": counts["awaiting"],
        "tradeable_liquidity": TRADEABLE_LIQUIDITY,
    }
