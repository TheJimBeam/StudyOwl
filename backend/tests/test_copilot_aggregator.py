"""
Tests for the Co-Pilot aggregator — pure helpers only.

The SQL detectors are intentionally not exercised here (no in-memory PG +
no real-DB fixture in this repo). What we pin:
  - Floors gate sparse data — no patterns at low cohort / low ratio.
  - Ranking dedupes by (subject, concept) with signal-priority tie-break.
  - Headlines stay in ratio language (never directive).
"""

import pytest

from config import settings
from services import copilot_aggregator
from services.copilot_aggregator import PatternCandidate


def _row(subject, concept, label, affected):
    """Mimic the SqlAlchemy Row that `_shape_candidates` consumes."""
    class _R:
        pass
    r = _R()
    r.subject = subject
    r.concept = concept
    r.label = label
    r.affected = affected
    return r


# ── floors ───────────────────────────────────────────────────────────────────


def test_shape_candidates_skips_below_cohort_floor():
    """affected_count < min_cohort_size is dropped, even at high ratio."""
    rows = [_row("math", "quadratic-factoring", "Quadratic factoring", 2)]
    cohort = {"math": 4}  # 2/4 = 0.5 — high ratio, low N
    out = copilot_aggregator._shape_candidates("level3_stuck", rows, cohort)
    assert out == []


def test_shape_candidates_skips_below_ratio_floor():
    """Even at high N, sub-30% ratio is dropped."""
    rows = [_row("math", "quadratic-factoring", "Quadratic factoring", 3)]
    cohort = {"math": 100}  # 3/100 = 0.03
    out = copilot_aggregator._shape_candidates("level3_stuck", rows, cohort)
    assert out == []


def test_shape_candidates_skips_when_cohort_unknown():
    """If a subject has zero sessions this week, no patterns can be ratioed."""
    rows = [_row("history", "thirty-years-war", "Thirty Years' War", 5)]
    cohort = {}  # no sessions in history this week
    out = copilot_aggregator._shape_candidates("concept_struggle", rows, cohort)
    assert out == []


def test_shape_candidates_keeps_above_floor():
    rows = [_row("math", "quadratic-factoring", "Quadratic factoring", 14)]
    cohort = {"math": 28}
    out = copilot_aggregator._shape_candidates("level3_stuck", rows, cohort)
    assert len(out) == 1
    assert out[0].affected_count == 14
    assert out[0].cohort_count == 28
    assert out[0].affected_ratio == pytest.approx(0.5)


# ── ranking + dedupe ─────────────────────────────────────────────────────────


def _cand(signal, subject, concept, ratio, affected=10, cohort=20):
    return PatternCandidate(
        signal_kind=signal,
        subject=subject,
        concept=concept,
        concept_label=(concept or "").replace("-", " ").title() or None,
        affected_count=affected,
        cohort_count=cohort,
        affected_ratio=ratio,
    )


def test_rank_dedupes_same_subject_concept_keeping_higher_priority_signal():
    """level3_stuck > repeated_failure > concept_struggle, by design."""
    candidates = [
        _cand("concept_struggle", "math", "quadratic-factoring", 0.6),
        _cand("level3_stuck", "math", "quadratic-factoring", 0.5),
    ]
    top = copilot_aggregator.rank_and_top_n(candidates, n=3)
    assert len(top) == 1
    assert top[0].signal_kind == "level3_stuck"


def test_rank_ties_break_by_higher_ratio():
    candidates = [
        _cand("repeated_failure", "math", "linear-equations", 0.4),
        _cand("repeated_failure", "math", "linear-equations", 0.6),
    ]
    top = copilot_aggregator.rank_and_top_n(candidates, n=3)
    assert len(top) == 1
    assert top[0].affected_ratio == pytest.approx(0.6)


def test_rank_sorts_top_n_by_ratio_desc():
    candidates = [
        _cand("concept_struggle", "math", "a", 0.30),
        _cand("concept_struggle", "math", "b", 0.50),
        _cand("concept_struggle", "math", "c", 0.40),
        _cand("concept_struggle", "math", "d", 0.60),
    ]
    top = copilot_aggregator.rank_and_top_n(candidates, n=2)
    assert [c.concept for c in top] == ["d", "b"]


def test_rank_returns_empty_when_no_candidates():
    assert copilot_aggregator.rank_and_top_n([], n=3) == []


# ── headlines ────────────────────────────────────────────────────────────────


def test_build_headline_uses_ratio_language_for_level3():
    c = _cand("level3_stuck", "math", "quadratic-factoring", 0.5, affected=14, cohort=28)
    text = copilot_aggregator.build_headline(c)
    assert "14 of 28 students hit Level 3" in text
    assert "Quadratic Factoring" in text
    assert "Re-teach" not in text  # no directive language


def test_build_headline_falls_back_to_subject_when_no_concept():
    c = _cand("level3_stuck", "math", None, 0.4, affected=8, cohort=20)
    c.concept_label = None
    text = copilot_aggregator.build_headline(c)
    assert "in math" in text
    assert "8 of 20" in text


def test_build_headline_repeated_failure_phrasing():
    c = _cand("repeated_failure", "english", "subject-verb-agreement", 0.3, affected=6, cohort=20)
    text = copilot_aggregator.build_headline(c)
    assert "repeated unresolved sessions" in text


# ── config knobs reflected in behavior ───────────────────────────────────────


def test_floors_respect_config_overrides(monkeypatch):
    monkeypatch.setattr(settings, "copilot_min_cohort_size", 1)
    monkeypatch.setattr(settings, "copilot_min_affected_ratio", 0.0)
    rows = [_row("math", "x", "X", 1)]
    cohort = {"math": 100}
    out = copilot_aggregator._shape_candidates("concept_struggle", rows, cohort)
    assert len(out) == 1
