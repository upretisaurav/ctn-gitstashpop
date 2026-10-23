"""LLM access: Claude (Anthropic API) first, the competition's qwen as a fallback,
and a deterministic mock for offline tests. Tracks spend against a daily budget."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Optional

log = logging.getLogger("ctnbot.llm")

# USD per million tokens: (input, output, cache_read). From the Anthropic pricing page, Oct 2026.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
}


class LLM:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self._anthropic = None
        self._comp = None
        self._comp_lock = threading.Lock()
        self._comp_last = 0.0
        self._claude_off_until = 0.0
        if cfg.anthropic_api_key:
            import anthropic
            self._anthropic = anthropic.Anthropic(api_key=cfg.anthropic_api_key, max_retries=3)
        if cfg.use_competition_llm_fallback and cfg.team_code and not cfg.fake_platform:
            try:
                import capturethenarrative as ctn
                self._comp = ctn.CompetitorLLM(team_code=cfg.team_code)
            except Exception as e:  # pragma: no cover
                log.warning("competition LLM unavailable: %s", e)

    # ------------------------------------------------------------------
    def budget_left(self) -> float:
        return self.cfg.daily_llm_budget_usd - self.db.llm_cost_today()

    def complete(self, system: str, user: str, *, smart: bool = False, max_tokens: int = 300,
                 cache_system: bool = True, temperature: float = 0.9) -> tuple[str, str]:
        """Return (text, model_used). Falls back gracefully; returns ("", "none") if nothing works."""
        model = self.cfg.model_smart if smart else self.cfg.model_fast
        if self._anthropic is not None and time.time() >= self._claude_off_until:
            if smart and self.budget_left() < 0.5:
                model = self.cfg.model_fast          # protect the budget for the rest of the day
            if self.budget_left() > 0.05:
                try:
                    return self._claude(model, system, user, max_tokens, cache_system, temperature), model
                except Exception as e:
                    msg = str(e).lower()
                    if any(w in msg for w in ("credit balance", "billing", "spend limit", "usage limit",
                                              "authentication", "invalid x-api-key", "permission")):
                        self._claude_off_until = time.time() + 3600
                        log.error("Claude unavailable (%s) — using the competition LLM for the next hour", e)
                    else:
                        log.warning("Claude call failed (%s): %s", model, e)
        if self._comp is not None:
            try:
                return self._qwen(system, user, max_tokens), "qwen2.5-3b-instruct"
            except Exception as e:
                log.warning("competition LLM failed: %s", e)
        if self.cfg.fake_platform or not self.cfg.anthropic_api_key:
            return mock_completion(system, user), "mock"
        return "", "none"

    def _claude(self, model, system, user, max_tokens, cache_system, temperature) -> str:
        sys_block = [{"type": "text", "text": system}]
        if cache_system:
            sys_block[0]["cache_control"] = {"type": "ephemeral"}
        # Claude 5.5 models think adaptively by default and thinking counts towards max_tokens.
        # Short social posts don't need it: Haiku can switch it off; Sonnet's lowest is "between_tools"
        # (no tools here, so effectively none). Low effort keeps it fast and cheap.
        if "haiku" in model:
            extra = {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}}
        elif "sonnet" in model:
            extra = {"thinking": {"type": "between_tools"}, "output_config": {"effort": "low"}}
        else:
            extra = {"output_config": {"effort": "low"}}
        budget = max_tokens + 600          # headroom in case some thinking still happens
        try:
            msg = self._anthropic.messages.create(
                model=model, max_tokens=budget, system=sys_block,
                messages=[{"role": "user", "content": user}], extra_body=extra)
        except Exception as e:
            if getattr(e, "status_code", None) != 400:
                raise
            log.info("retrying %s without thinking/effort settings (%s)", model, str(e)[:120])
            msg = self._anthropic.messages.create(
                model=model, max_tokens=budget, system=sys_block, messages=[{"role": "user", "content": user}])
        u = msg.usage
        pin, pout, pcache = PRICES.get(model, PRICES["claude-sonnet-5-5"])
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        cost = ((u.input_tokens * pin) + (cache_write * pin * 1.25) + (cache_read * pcache)
                + (u.output_tokens * pout)) / 1e6
        self.db.add_llm_cost(model, cost)
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()

    def _qwen(self, system, user, max_tokens) -> str:
        # Team-wide 3/min and one at a time.
        with self._comp_lock:
            wait = 21 - (time.time() - self._comp_last)
            if wait > 0:
                time.sleep(wait)
            self._comp_last = time.time()
            return self._comp.ask(user[-6000:], system=system[-6000:], max_tokens=min(max_tokens, 300)).strip()


# ----------------------------------------------------------------------------
def extract_json(text: str):
    """Pull the first JSON object/array out of a model reply."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    pairs = sorted((("[", "]"), ("{", "}")), key=lambda oc: (text.find(oc[0]) == -1, text.find(oc[0])))
    for opener, closer in pairs:
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    return None


def mock_completion(system: str, user: str) -> str:
    """Deterministic stand-in so the whole pipeline runs offline."""
    if "CLASSIFY_ACCOUNTS" in user:
        ids = re.findall(r"ACCOUNT (\d+)", user)
        out = []
        for i in ids:
            n = int(i)
            out.append({"id": n, "npc": 0.9 if n < 6000 else 0.02, "stance": (n % 5) - 2, "stance_vacc": 1,
                        "stance_yearly": 0, "side_pushed": "no" if n % 2 else "yes",
                        "concerns": ["sunset", "privacy"] if n % 2 else ["jobs_income"],
                        "identity": "market stall holder", "persuadable": 0.6})
        return json.dumps(out)
    if "CURRENT STRATEGY AND RESULTS" in user:
        return json.dumps({"rationale": "mock", "changes": {"share_top_level": 0.9, "argument_weights":
                           {"hospital_capacity": 1.5, "made_up_arg": 2}, "style_notes": ["Keep it short."],
                           "min_report_confidence": 0.5}})
    if "JUDGE_SHIFT" in user:
        return json.dumps({"shift": 0.5, "moved": True})
    import random as _r
    bits = ["my nan", "the ferry crew", "our corner shop", "Regatta Week", "the night shift", "my kids' school",
            "the outer islands clinic", "the dive shop", "the market stall", "my neighbour on oxygen"]
    tail = f" ({_r.choice(bits)}, {_r.randint(1, 10_000)})"
    if "WRITE_POST" in user:
        return "Thinking about " + _r.choice(bits) + " today. I'd rather we never need the surge plan again." + tail
    return "Fair point. For me it comes down to " + _r.choice(bits) + " - nothing kicks in until everyone's had a free shot." + tail
