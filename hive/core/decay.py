"""
Phase 4: confidence decay — pure read-time functions, no DB mutation.

A decision's *effective* confidence decays exponentially with the time since it
was last written/reinforced (created_at doubles as the last-reinforced clock):

    eff_conf = stored_confidence * 0.5 ** (age_days / HALF_LIFE_DAYS)

Stored confidence is never mutated by decay — it is recomputed per query, so the
behaviour is deterministic and replayable with no background job. Reinforcement
(see writer.reinforce_decision) bumps stored confidence and resets created_at,
restarting the half-life clock from now.
"""

from __future__ import annotations

from datetime import datetime, timezone

HALF_LIFE_DAYS = 90.0   # confidence halves every 90 unreinforced days
CONF_CAP       = 1.0    # confidence ceiling. Capped at 1.0 (not 2.0) so a heavily
                        # reinforced decision can't buy immunity from decay: post-
                        # abandonment warmth is always bounded to the base 180-day
                        # schedule no matter how many times it was touched. Confidence
                        # is a freshness/trust signal in [0,1], never a reserve.
ARCHIVE_FLOOR  = 0.25   # eff_conf below this → eligible for cold archive (≈180 days
                        # after the last write/reinforce at full confidence)
REINFORCE_STEP = 0.25   # default reinforcement bump (toward the 1.0 ceiling)
CONTRA_SIM     = 0.80   # dense cosine threshold for contradiction v2


def clamp_confidence(x: float | None) -> float:
    """Confidence lives in [0, 1]. Used on every write + reinforcement."""
    if x is None:
        return 1.0
    return max(0.0, min(1.0, float(x)))


def _parse_iso(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def age_days(created_at: str, now: datetime | None = None) -> float:
    """Whole+fractional days since created_at. 0.0 if unparseable or in the future."""
    dt = _parse_iso(created_at)
    if dt is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    secs = (now - dt).total_seconds()
    return max(0.0, secs / 86400.0)


def effective_confidence(stored: float | None, created_at: str,
                         now: datetime | None = None) -> float:
    """
    Decayed confidence at read time. age 0 → stored (day-0 behaviour preserved).
    """
    conf = 1.0 if stored is None else float(stored)
    a = age_days(created_at, now)
    if a <= 0.0:
        return conf
    return conf * (0.5 ** (a / HALF_LIFE_DAYS))
