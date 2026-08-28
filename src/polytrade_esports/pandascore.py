"""PandaScore live-state adapter for CS2.

Polymarket's own Gamma metadata carries ``pandascoreMatchId`` for every CS2
event, so the join between a market and a live match is exact rather than a
name-similarity guess.

Response parsing is deliberately tolerant: PandaScore plan tiers differ in how
much of a running match they expose, and every field this module reads is
optional. When in-map round detail is unavailable the adapter still returns a
maps-only state instead of failing the whole tick.

UNVERIFIED against a live key at time of writing; ``probe`` exists to check the
real response shape once a token is configured.
"""

import json
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .timeutil import canonical_timestamp, isoformat, utc_now
from .types import LiveState

PANDASCORE_BASE_URL = "https://api.pandascore.co"
SOURCE = "pandascore"


class PandaScoreError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class PandaScoreClient:
    def __init__(
        self,
        token: str,
        base_url: str = PANDASCORE_BASE_URL,
        timeout: float = 15.0,
    ) -> None:
        if not str(token or "").strip():
            raise ValueError("PandaScore token is required")
        self.token = str(token).strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = "%s%s" % (self.base_url, path)
        if params:
            url = "%s?%s" % (url, urlencode(params))
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer %s" % self.token,
                "User-Agent": "polytrade-esports-live/0.2",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = ""
            try:
                detail = error.read().decode("utf-8")[:300]
            except Exception:  # pragma: no cover - best-effort diagnostics
                detail = ""
            raise PandaScoreError(
                "PandaScore %s returned %d %s" % (path, error.code, detail),
                status=error.code,
            ) from error

    def running_matches(self, per_page: int = 50) -> List[Dict[str, Any]]:
        payload = self._get("/csgo/matches/running", {"per_page": per_page})
        return payload if isinstance(payload, list) else []

    def match(self, match_id: Any) -> Dict[str, Any]:
        payload = self._get("/csgo/matches/%s" % match_id)
        return payload if isinstance(payload, dict) else {}

    def game(self, game_id: Any) -> Dict[str, Any]:
        payload = self._get("/csgo/games/%s" % game_id)
        return payload if isinstance(payload, dict) else {}

    def probe(self) -> Dict[str, Any]:
        """Report what this token can actually see. Run once after setup."""
        report: Dict[str, Any] = {"running_matches": None, "game_detail": None}
        try:
            running = self.running_matches(per_page=5)
            report["running_matches"] = {"ok": True, "count": len(running)}
        except PandaScoreError as error:
            report["running_matches"] = {"ok": False, "error": str(error)}
            return report
        game_id = None
        for match in running:
            for game in match.get("games") or []:
                if game.get("status") == "running":
                    game_id = game.get("id")
                    break
            if game_id:
                break
        if game_id is None:
            report["game_detail"] = {"ok": None, "reason": "no running game to probe"}
            return report
        try:
            detail = self.game(game_id)
            report["game_detail"] = {
                "ok": True,
                "game_id": game_id,
                "keys": sorted(detail.keys()),
                "has_rounds": bool(detail.get("rounds")),
                "has_teams": bool(detail.get("teams")),
            }
        except PandaScoreError as error:
            report["game_detail"] = {"ok": False, "error": str(error)}
        return report


def _team_ids(match: Dict[str, Any]) -> List[Any]:
    ids: List[Any] = []
    for entry in match.get("opponents") or []:
        opponent = entry.get("opponent") if isinstance(entry, dict) else None
        if isinstance(opponent, dict) and opponent.get("id") is not None:
            ids.append(opponent.get("id"))
    return ids


def _score_by_team(match: Dict[str, Any]) -> Dict[Any, int]:
    scores: Dict[Any, int] = {}
    for result in match.get("results") or []:
        if not isinstance(result, dict):
            continue
        team_id = result.get("team_id")
        if team_id is None:
            continue
        try:
            scores[team_id] = int(result.get("score") or 0)
        except (TypeError, ValueError):
            scores[team_id] = 0
    return scores


def _running_game(match: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    games = [game for game in match.get("games") or [] if isinstance(game, dict)]
    for game in games:
        if game.get("status") == "running":
            return game
    return None


def _round_scores(game: Dict[str, Any], team_ids: List[Any]) -> Dict[Any, int]:
    """Read per-team round counts from whichever shape the plan returns."""
    scores: Dict[Any, int] = {}
    for team in game.get("teams") or []:
        if not isinstance(team, dict):
            continue
        team_id = team.get("team_id", team.get("id"))
        value = team.get("score", team.get("rounds_won"))
        if team_id is None or value is None:
            continue
        try:
            scores[team_id] = int(value)
        except (TypeError, ValueError):
            continue
    if scores:
        return scores
    rounds = game.get("rounds")
    if isinstance(rounds, list) and rounds:
        counted: Dict[Any, int] = {team_id: 0 for team_id in team_ids}
        for entry in rounds:
            if not isinstance(entry, dict):
                continue
            winner = entry.get("winner_team", entry.get("winner"))
            if isinstance(winner, dict):
                winner = winner.get("id")
            if winner in counted:
                counted[winner] += 1
        if any(counted.values()):
            return counted
    return {}


def _has_round_shape(game: Dict[str, Any]) -> bool:
    """Whether a provider payload actually exposes round-level fields."""
    for team in game.get("teams") or []:
        if isinstance(team, dict) and (
            team.get("score") is not None or team.get("rounds_won") is not None
        ):
            return True
    return isinstance(game.get("rounds"), list)


def _map_label(game: Dict[str, Any]) -> str:
    """Name the current map, or fall back to its position in the series.

    Map names live on the per-game record, which is plan-gated. The running
    match list only exposes ``position``, so "MAP 2" is the honest answer
    rather than "unknown".
    """
    map_info = game.get("map")
    if isinstance(map_info, dict) and map_info.get("name"):
        return str(map_info["name"])
    if isinstance(map_info, str) and map_info.strip():
        return map_info.strip()
    position = game.get("position")
    if position is not None:
        try:
            return "MAP %d" % int(position)
        except (TypeError, ValueError):
            pass
    return "unknown"


def _current_side_advantage(game: Dict[str, Any], team_id: Any) -> float:
    """Small CT-side edge when the current side is known, else neutral.

    CS2 CT sides win marginally more rounds on most of the active pool. The
    magnitude is intentionally conservative; it is a nudge, not a model.
    """
    rounds = game.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        return 0.0
    last = rounds[-1]
    if not isinstance(last, dict):
        return 0.0
    ct_team = last.get("ct_team", last.get("ct"))
    if isinstance(ct_team, dict):
        ct_team = ct_team.get("id")
    if ct_team is None:
        return 0.0
    return 0.04 if ct_team == team_id else -0.04


def build_state(
    match_id: str,
    match: Dict[str, Any],
    team_a_index: int = 0,
    game_detail: Optional[Dict[str, Any]] = None,
) -> Optional[LiveState]:
    """Turn a PandaScore match payload into a normalized ``LiveState``.

    ``team_a_index`` selects which PandaScore opponent maps to the Polymarket
    "A" outcome; the caller resolves that from the market's outcome order.
    Returns ``None`` when opponents cannot be identified.
    """
    team_ids = _team_ids(match)
    if len(team_ids) != 2:
        return None
    id_a = team_ids[team_a_index]
    id_b = team_ids[1 - team_a_index]
    map_scores = _score_by_team(match)
    maps_a = int(map_scores.get(id_a, 0))
    maps_b = int(map_scores.get(id_b, 0))

    # The running-match payload always carries the game list; game_detail is the
    # richer per-map record, which lower plan tiers refuse (403). Merge both so
    # round scores appear when available and the map position still resolves
    # when they do not.
    listed = _running_game(match) or {}
    game = dict(listed)
    if game_detail:
        game.update(game_detail)

    rounds_a = 0
    rounds_b = 0
    current_map = "unknown"
    side_advantage_a = 0.0
    if game:
        round_scores = _round_scores(game, team_ids)
        rounds_a = int(round_scores.get(id_a, 0))
        rounds_b = int(round_scores.get(id_b, 0))
        current_map = _map_label(game)
        side_advantage_a = _current_side_advantage(game, id_a)

    source_at = match.get("modified_at") or match.get("begin_at")
    try:
        source_at = canonical_timestamp(str(source_at)) if source_at else None
    except ValueError:
        source_at = None
    observed_at = isoformat(utc_now())
    # PandaScore stamps can lag or lead the local clock; never let a provider
    # timestamp become "future" relative to our own observation.
    if source_at is None or source_at > observed_at:
        source_at = observed_at

    return LiveState(
        match_id=match_id,
        source_at=source_at,
        observed_at=observed_at,
        maps_a=maps_a,
        maps_b=maps_b,
        rounds_a=rounds_a,
        rounds_b=rounds_b,
        current_map=current_map,
        side_advantage_a=side_advantage_a,
        source=SOURCE,
        raw={
            "match": match,
            "game": game or None,
            "round_detail_available": bool(game_detail) or _has_round_shape(game),
        },
    ).normalized()


def team_a_index(match: Dict[str, Any], team_a_name: str) -> int:
    """Match the Polymarket "A" team name to a PandaScore opponent slot."""
    names: List[str] = []
    for entry in match.get("opponents") or []:
        opponent = entry.get("opponent") if isinstance(entry, dict) else None
        names.append(str((opponent or {}).get("name") or "").strip().lower())
    target = str(team_a_name or "").strip().lower()
    if len(names) != 2:
        return 0
    if target and target == names[0]:
        return 0
    if target and target == names[1]:
        return 1
    # Fall back to token overlap for cosmetic naming differences
    # ("ex-MIBR Academy" vs "MIBR Academy").
    target_tokens = set(target.split())
    overlaps = [len(target_tokens & set(name.split())) for name in names]
    if overlaps[1] > overlaps[0]:
        return 1
    return 0
