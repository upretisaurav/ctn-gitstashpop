"""Tunable strategy, with hard bounds.

Two layers, merged every minute while the bot runs:
  1. strategy.json in the repo  — the baseline. Humans (or the scheduled Claude
     review) edit it and push; Railway redeploys.
  2. the in-bot strategist      — every few hours Claude reviews recent results and
     writes an overlay into the database. Keys listed in strategy.json "lock" are
     never touched by it.

Every value is clamped to the bounds below, so no review can push the bot into
something reckless (e.g. reporting at low confidence or mass-mentioning)."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from .knowledge import ARGUMENTS, CONCERNS, PERSONA_BY_KEY

log = logging.getLogger("ctnbot.strategy")

REPO_FILE = Path(__file__).resolve().parent.parent / "strategy.json"

DEFAULTS: dict[str, Any] = {
    "style_notes": [],
    "avoid_phrases": [],
    "argument_weights": {},
    "persona_weights": {},
    "concern_weights": {},
    "stance_weights": {"-2": 0.2, "-1": 1.0, "0": 1.0, "1": 0.55, "2": 0.1},
    "share_top_level": 0.15,
    "extra_mentions_per_reply": 2,
    "mentions_per_post": 4,
    "boost_likes_per_post": 2,
    "min_report_confidence": 0.85,
    "max_pending_reports": 24,
    "reply_fresh_seconds": 300,
    "target_active_accounts": 12,
    "smart_reply_min_persuadable": 0.55,
    "smart_all_writing": 0.0,
    "gif_rate": 0.2,
    "hashtag_rate": 0.4,
    "lowercase_rate": 0.12,
    "like_reply_targets": 0.6,
    "follow_engaged": 0.5,
    "lock": [],
}

NUM_BOUNDS = {
    "share_top_level": (0.0, 0.4),
    "extra_mentions_per_reply": (0, 3),
    "mentions_per_post": (0, 4),
    "boost_likes_per_post": (0, 8),
    "min_report_confidence": (0.75, 0.97),
    "max_pending_reports": (0, 25),
    "reply_fresh_seconds": (90, 900),
    "target_active_accounts": (1, 50),
    "smart_reply_min_persuadable": (0.0, 1.0),
    "smart_all_writing": (0.0, 1.0),
    "gif_rate": (0.0, 0.5),
    "hashtag_rate": (0.0, 0.8),
    "lowercase_rate": (0.0, 0.3),
    "like_reply_targets": (0.0, 1.0),
    "follow_engaged": (0.0, 1.0),
}
INT_KEYS = {"extra_mentions_per_reply", "mentions_per_post", "boost_likes_per_post", "max_pending_reports",
            "reply_fresh_seconds", "target_active_accounts"}
WEIGHT_BOUNDS = (0.2, 3.0)
STANCE_BOUNDS = (0.0, 2.0)
WEIGHT_VOCAB = {"argument_weights": set(ARGUMENTS), "persona_weights": set(PERSONA_BY_KEY),
                "concern_weights": set(CONCERNS), "stance_weights": {"-2", "-1", "0", "1", "2"}}


def clamp(raw: dict | None, *, strict_keys: bool = True) -> dict:
    """Return a cleaned copy of raw: unknown keys dropped, every value bounded."""
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            if k in NUM_BOUNDS:
                lo, hi = NUM_BOUNDS[k]
                x = max(lo, min(hi, float(v)))
                out[k] = int(round(x)) if k in INT_KEYS else round(x, 3)
            elif k in WEIGHT_VOCAB:
                lo, hi = STANCE_BOUNDS if k == "stance_weights" else WEIGHT_BOUNDS
                out[k] = {str(kk): round(max(lo, min(hi, float(vv))), 3)
                          for kk, vv in (v or {}).items() if str(kk) in WEIGHT_VOCAB[k]}
            elif k == "style_notes":
                out[k] = [str(s)[:220] for s in (v or []) if str(s).strip()][:10]
            elif k == "avoid_phrases":
                out[k] = [str(s)[:80] for s in (v or []) if str(s).strip()][:25]
            elif k == "lock":
                out[k] = [str(s) for s in (v or []) if str(s) in DEFAULTS]
            elif not strict_keys:
                out[k] = v
        except (TypeError, ValueError):
            continue
    return out


class Strategy:
    def __init__(self, db, path: Path = REPO_FILE):
        self.db = db
        self.path = Path(os.environ.get("CTN_STRATEGY_FILE", path))
        self._data = dict(DEFAULTS)
        self._loaded_at = 0.0
        self.reload()

    def reload(self) -> None:
        repo = {}
        if self.path.exists():
            try:
                repo = clamp(json.loads(self.path.read_text()))
            except ValueError as e:
                log.error("strategy.json is not valid JSON (%s) — using defaults", e)
        auto = clamp(self.db.get("strategy_auto", {}) or {})
        locked = set(repo.get("lock", []))
        merged = dict(DEFAULTS)
        merged.update(repo)
        for k, v in auto.items():
            if k in locked or k == "lock":
                continue
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}
            elif k in ("style_notes", "avoid_phrases"):
                merged[k] = list(dict.fromkeys(list(merged.get(k, [])) + list(v)))[:12 if k == "style_notes" else 30]
            else:
                merged[k] = v
        self._data = merged
        self._loaded_at = time.time()

    def maybe_reload(self, every: float = 60.0) -> None:
        if time.time() - self._loaded_at >= every:
            self.reload()

    def __getitem__(self, key: str):
        return self._data.get(key, DEFAULTS.get(key))

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def weight(self, table: str, key) -> float:
        return float((self._data.get(table) or {}).get(str(key), 1.0))

    def as_dict(self) -> dict:
        return dict(self._data)
