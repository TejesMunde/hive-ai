"""
Phase 6: pure commit-decision extractor — the quality floor in front of the guard.

This module is intentionally PURE: no git calls, no DB, no I/O. It turns raw commit
text into a `Candidate` decision, or returns `None` when the commit does not clear
the floor. That makes the hardest, lowest-precision component of Phase 6 the most
testable — every gate and every skip reason has a unit test, no subprocess needed.

The floor runs BEFORE the write guard (it is additive, never a bypass). Its job is
to keep low-signal commits from ever reaching staging. It is tuned for PRECISION
over recall: we would rather miss a decision than flood memory with diff noise.

A commit becomes a candidate only if ALL hold:
  1. type gate   — conventional prefix in {feat,fix,refactor,perf} OR (no prefix)
                   a >=5-word subject. chore/docs/style/test/build/ci, merges and
                   version bumps are skipped.
  2. cue gate    — message carries explicit decision language (_DECISION_CUES),
                   reusing the opposition/replacement vocabulary the guard's
                   contradiction detector already trusts.
  3. substance   — extracted `what` >= MIN_WHAT_WORDS and a non-empty `why` is
                   found, so the survivor also clears the guard on its own merits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MIN_WHAT_WORDS = 5

# Conventional-commit types worth extracting a decision from. chore/docs/style/
# test/build/ci are deliberately excluded — they are rarely architectural.
_DECISION_TYPES = {"feat", "fix", "refactor", "perf"}

# Decision language. Kept aligned with guard._REPLACE_CUES so the floor admits the
# same opposition/replacement vocabulary the contradiction detector understands.
_DECISION_CUES = (
    "chose", "chosen", " over ", "switched to", "switch to", "instead of",
    "rather than", "because", "replaced", "replace ", "migrated to", "migrate to",
    "decided to", "decided ", "opted for", "opted ", "in favor of", "moved to",
    "adopted", "settled on", "going with", "went with",
)

_CONVENTIONAL = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?(?P<bang>!)?:\s*(?P<rest>.+)$")
_MERGE = re.compile(r"^(merge\b|revert\b)", re.IGNORECASE)
# Version-bump subjects like "bump to 1.2.3", "v2.0.0", "release 3.1".
_VERSION_BUMP = re.compile(
    r"^\s*(bump|release|version|v?\d+\.\d+)\b.*$|\bv?\d+\.\d+\.\d+\b", re.IGNORECASE
)


@dataclass(frozen=True)
class CommitInfo:
    """Parsed commit: subject line + body, plus the conventional-commit type."""
    subject: str
    body: str
    ctype: str | None    # conventional prefix (e.g. 'feat'), or None


@dataclass(frozen=True)
class Candidate:
    """A decision the floor judges worth offering to write_memory."""
    what: str
    why: str
    skip_reason: None = None


@dataclass(frozen=True)
class Skip:
    """The floor dropped the commit. reason ∈ {type_gate, no_cue, too_thin}."""
    reason: str


def parse_commit(raw: str) -> CommitInfo:
    """
    Split raw commit message text into subject + body and detect a conventional
    prefix. `raw` is the commit message only (NOT the diff) — first line is the
    subject, the rest (after a blank line, conventionally) is the body.
    """
    lines = (raw or "").replace("\r\n", "\n").split("\n")
    subject = lines[0].strip() if lines else ""
    body = "\n".join(lines[1:]).strip()

    ctype = None
    m = _CONVENTIONAL.match(subject)
    if m:
        ctype = m.group("type").lower()
    return CommitInfo(subject=subject, body=body, ctype=ctype)


def _strip_prefix(subject: str) -> str:
    """Return the subject with any conventional-commit prefix removed."""
    m = _CONVENTIONAL.match(subject)
    return m.group("rest").strip() if m else subject.strip()


def _word_count(s: str) -> int:
    return len(s.split())


def _first_paragraph(body: str) -> str:
    """First non-empty paragraph of the body, newlines folded to spaces."""
    for block in re.split(r"\n\s*\n", body or ""):
        block = " ".join(block.split())
        if block:
            return block
    return ""


def _has_cue(text: str) -> bool:
    low = (text or "").lower()
    return any(cue in low for cue in _DECISION_CUES)


def extract_decision(info: CommitInfo) -> Candidate | Skip:
    """
    Apply the three floor gates to a parsed commit. Returns a Candidate when the
    commit clears the floor, else a Skip with the failing gate's reason.
    """
    subject = info.subject or ""

    # ── Gate 1: type ─────────────────────────────────────────────────────────
    if _MERGE.match(subject):
        return Skip("type_gate")
    what = _strip_prefix(subject)
    if _VERSION_BUMP.match(what):
        return Skip("type_gate")
    if info.ctype is not None:
        # Conventional commit: only the decision-bearing types pass.
        if info.ctype not in _DECISION_TYPES:
            return Skip("type_gate")
    else:
        # No conventional prefix: require a substantial subject to even consider.
        if _word_count(what) < MIN_WHAT_WORDS:
            return Skip("type_gate")

    # ── Gate 2: decision cue (subject OR body) ───────────────────────────────
    if not (_has_cue(what) or _has_cue(info.body)):
        return Skip("no_cue")

    # ── Gate 3: substance — what >= MIN words, and a real why exists ─────────
    if _word_count(what) < MIN_WHAT_WORDS:
        return Skip("too_thin")
    why = _first_paragraph(info.body)
    if not why:
        # No body — fall back to the subject itself only if it states the reason
        # ("... because ..."). Otherwise a decision with no "why" is exactly what
        # the guard rejects, so drop it at the floor instead of flooding staging.
        why = what if _has_cue(what) else ""
    if _word_count(why) < MIN_WHAT_WORDS:
        return Skip("too_thin")

    return Candidate(what=what, why=why)
