"""All tunables in one place. Values come from environment variables (or a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass
class Config:
    team_code: str = ""
    side: str = "yes"                      # "yes" or "no"
    data_dir: str = "."                    # /data on Railway (a persistent volume)
    db_path: str = ""

    # Modes
    dry_run: bool = False                  # never write to Legit Social; log what would happen
    fake_platform: bool = False            # use the offline simulator instead of the real API
    observe_only: bool = False             # ingest + classify only, no posts or reports

    # LLMs
    anthropic_api_key: str = ""
    model_fast: str = "claude-haiku-5-5"   # classification, routine replies
    model_smart: str = "claude-sonnet-5-5" # conversations with NPCs who answered us
    daily_llm_budget_usd: float = 20.0     # Sonnet for important replies needs ~$8-10/day at 3 writes/min
    use_competition_llm_fallback: bool = True

    # Accounts
    target_active_accounts: int = 12       # grow this towards 50 once things work
    max_active_accounts: int = 50
    max_total_accounts: int = 200
    retire_after_actions: int = 80         # retire an exposed account well before 100 actions (platform counts more than we see)

    # Red team
    reply_fresh_seconds: int = 300         # only reply to NPC posts younger than this
    max_thread_depth: int = 7              # NPCs rarely reply past 8 deep
    target_cooldown_minutes: int = 45      # don't hit the same NPC more often than this
    extra_mentions_per_reply: int = 2      # other active NPCs with the same worry (max 3)
    mentions_per_post: int = 4             # top-level post mentions (max 4)
    share_top_level: float = 0.15          # fraction of write slots for top-level posts
    boost_likes_per_post: int = 2          # teammate likes on our own top-level posts
    life_post_every: int = 18              # every Nth post from an account is a slice-of-life post

    # Blue team
    blue_enabled: bool = True
    max_pending_reports: int = 24          # platform cap is 25
    min_report_confidence: float = 0.85
    report_no_side_first: bool = True
    account_reports_enabled: bool = True

    # Pacing (per team, per minute) — mirrors the server; the server headers win if lower
    writes_per_min: int = 3
    draft_workers: int = 3                 # LLM drafts written in parallel so every write slot gets used
    draft_queue: int = 3                   # drafts in flight + ready at once (keeps them fresh)
    draft_max_age: int = 240               # seconds before an unsent reply draft is stale
    likes_per_min: int = 30
    follows_per_min: int = 30
    reports_per_min: int = 3
    feeds_per_min: int = 10
    reads_per_min: int = 250
    logins_per_min: int = 5

    log_level: str = "INFO"
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, env_file: str | None = ".env") -> "Config":
        if env_file:
            _load_dotenv(Path(env_file))
        c = cls()
        c.team_code = os.environ.get("CTN_TEAM_CODE", "").strip()
        c.side = os.environ.get("CTN_SIDE", c.side).strip().lower()
        c.data_dir = os.environ.get("CTN_DATA_DIR", c.data_dir)
        Path(c.data_dir).mkdir(parents=True, exist_ok=True)
        c.db_path = os.environ.get("CTN_DB") or str(Path(c.data_dir) / "ctnbot.sqlite3")
        c.dry_run = _b("CTN_DRY_RUN", c.dry_run)
        c.fake_platform = _b("CTN_FAKE_PLATFORM", c.fake_platform)
        c.observe_only = _b("CTN_OBSERVE_ONLY", c.observe_only)
        c.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        c.model_fast = os.environ.get("CTN_MODEL_FAST", c.model_fast)
        c.model_smart = os.environ.get("CTN_MODEL_SMART", c.model_smart)
        c.daily_llm_budget_usd = _f("CTN_DAILY_BUDGET_USD", c.daily_llm_budget_usd)
        c.use_competition_llm_fallback = _b("CTN_COMP_LLM_FALLBACK", c.use_competition_llm_fallback)
        c.target_active_accounts = _i("CTN_TARGET_ACTIVE", c.target_active_accounts)
        c.retire_after_actions = _i("CTN_RETIRE_AFTER_ACTIONS", c.retire_after_actions)
        c.reply_fresh_seconds = _i("CTN_REPLY_FRESH_SECONDS", c.reply_fresh_seconds)
        c.extra_mentions_per_reply = min(3, _i("CTN_EXTRA_MENTIONS", c.extra_mentions_per_reply))
        c.mentions_per_post = min(4, _i("CTN_POST_MENTIONS", c.mentions_per_post))
        c.share_top_level = _f("CTN_SHARE_TOP_LEVEL", c.share_top_level)
        c.boost_likes_per_post = _i("CTN_BOOST_LIKES", c.boost_likes_per_post)
        c.blue_enabled = _b("CTN_BLUE", c.blue_enabled)
        c.max_pending_reports = min(25, _i("CTN_MAX_PENDING_REPORTS", c.max_pending_reports))
        c.min_report_confidence = _f("CTN_MIN_REPORT_CONFIDENCE", c.min_report_confidence)
        c.account_reports_enabled = _b("CTN_ACCOUNT_REPORTS", c.account_reports_enabled)
        c.draft_workers = max(1, _i("CTN_DRAFT_WORKERS", c.draft_workers))
        c.draft_queue = max(1, _i("CTN_DRAFT_QUEUE", c.draft_queue))
        c.log_level = os.environ.get("CTN_LOG_LEVEL", c.log_level)
        if c.side not in ("yes", "no"):
            raise ValueError("CTN_SIDE must be 'yes' or 'no'")
        return c

    def path(self, name: str) -> str:
        return str(Path(self.data_dir) / name)

    @property
    def side_label(self) -> str:
        return "Vote Yes" if self.side == "yes" else "Vote No"
