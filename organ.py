#!/usr/bin/env python3
"""
CI Runner Health Organ — extracted decision logic from discovery-engine.

A pure decider that judges whether the self-hosted ``dfa-ci`` GitHub Actions
runner pool is SATURATING — i.e. whether workflow runs are backing up in queue
waiting for a runner to pick them up. It computes the p90 of the queue-wait
(``run_started_at - created_at``) over a rolling window of recent runs and
returns a demand-driven "scale the VPS runner pool" verdict when that p90
exceeds a threshold.

This is the pure core of discovery-engine's ``app/services/ci_runner_health.py``
(``compute_queue_wait`` + ``check_ci_runner_health``). The impure parts are the
spine's job:
  * fetching the recent ``ci.yml`` workflow runs from the GitHub REST API
    (``_fetch_ci_runs``) — the organ is *handed* the runs in ``state``,
  * inserting the ``ci_runner_saturation`` PendingWidgetAction + dedup
    (``maybe_alert_ci_runner_saturation``) — the organ only *advises*,
  * reading thresholds from environment variables — passed in ``context``,
  * reading the wall clock — passed in ``state.now`` (or derived from the
    freshest run for determinism).

Contract (see CONTRACT.md):
  INPUT state: {
    "runs": [                                       # raw ci.yml workflow runs
      {"created_at": "2026-06-03T10:00:00Z",        # when the run was queued
       "run_started_at": "2026-06-03T10:00:05Z"},   # when a runner picked it up
      ...
    ],
    "now": "2026-06-03T10:05:00Z" | null            # reference clock for the
                                                    # rolling window (optional;
                                                    # defaults to the freshest
                                                    # run's created_at)
  }

  INPUT context (all optional — organ works with context absent): {
    "threshold_seconds": 60,    # p90 queue-wait above this trips saturation
    "window_hours": 4,          # only runs created within this window count
    "min_sample": 5,            # below this many valid runs the p90 is noise
    "window_runs": 50           # informational: how many runs the spine fetched
  }

  OUTPUT: {
    "output": {
      "saturated": bool,            # is the runner pool backing up?
      "reason_skipped": str | null, # why no verdict (no_runs/insufficient_sample)
      "p90_seconds": float,
      "sample_size": int,
      "max_seconds": float,
      "threshold_seconds": float,
      "window_hours": float,
      "checked_at": str | null,
      "recommendation": str         # scale_runner_pool / hold / insufficient_data
    },
    "rationale": str,
    "self_metric": {
      "confidence": float,
      "decision_path": str,
      "sample_size": int,
      "p90_seconds": float
    }
  }

The organ is pure: all inputs via JSON, no DB/network/clock calls, deterministic,
fail-safe to the conservative verdict (``saturated=False``, low confidence) on
malformed or empty ``state`` — it never returns a confident-wrong "scale now".
Stdlib-only.
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

# Defaults mirror discovery-engine's env-var defaults
# (CI_QUEUE_WAIT_ALERT_SECONDS / _WINDOW_HOURS / _MIN_SAMPLE / _WINDOW_RUNS).
_DEFAULT_THRESHOLD_SECONDS = 60.0
_DEFAULT_WINDOW_HOURS = 4.0
_DEFAULT_MIN_SAMPLE = 5
_DEFAULT_WINDOW_RUNS = 50


def _parse_iso(value) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp ('2026-06-03T10:00:00Z') to an aware
    datetime. Returns None on missing/malformed input. Mirrors the source
    ``_parse_iso`` exactly."""
    if not value or not isinstance(value, str):
        return None
    try:
        # GitHub returns trailing 'Z'; fromisoformat needs +00:00.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _p90(values: list[float]) -> float:
    """p90 via the nearest-rank method on a sorted ascending list.

    rank = ceil(0.9 * n) (1-indexed). For n=1 -> the single value; for n=10 ->
    the 9th value. Deterministic and dependency-free. Mirrors the source ``_p90``.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    rank = max(1, math.ceil(0.9 * n))
    return float(ordered[rank - 1])


def _num(value, default: float) -> float:
    """Coerce a context value to float, falling back to ``default`` on
    None/garbage. Mirrors the try/except float() pattern in the source's
    env-var readers."""
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int(value, default: int, floor: int = 1) -> int:
    """Coerce a context value to a floored int, mirroring the source readers
    (``max(1, int(...))``)."""
    try:
        if value is None:
            return int(default)
        return max(floor, int(value))
    except (TypeError, ValueError):
        return int(default)


def decide(state: dict, context: dict | None = None) -> dict:
    """Judge whether the CI runner pool is saturating from a handed-in list of
    recent workflow runs. Pure, deterministic, fail-safe to ``saturated=False``."""
    context = context or {}

    try:
        threshold = _num(context.get("threshold_seconds"), _DEFAULT_THRESHOLD_SECONDS)
        window_hours = _num(context.get("window_hours"), _DEFAULT_WINDOW_HOURS)
        min_sample = _int(context.get("min_sample"), _DEFAULT_MIN_SAMPLE)
        window_runs = _int(context.get("window_runs"), _DEFAULT_WINDOW_RUNS)

        if not isinstance(state, dict) or not state:
            return _skip(
                "no_runs",
                "No state supplied — no CI runs to evaluate, holding (no scale signal).",
                confidence=0.3, decision_path="empty_state",
                threshold=threshold, window_hours=window_hours, window_runs=window_runs,
            )

        runs = state.get("runs")
        if not isinstance(runs, list) or not runs:
            return _skip(
                "no_runs",
                "No CI runs in window — runner pool is idle, no saturation signal.",
                confidence=0.3, decision_path="no_runs",
                threshold=threshold, window_hours=window_hours, window_runs=window_runs,
            )

        # Resolve the reference clock. The organ never reads the wall clock; the
        # spine passes state.now. For determinism without it, anchor the rolling
        # window to the freshest run's created_at.
        now = _parse_iso(state.get("now"))
        created_times = [
            _parse_iso(r.get("created_at"))
            for r in runs if isinstance(r, dict)
        ]
        created_times = [c for c in created_times if c is not None]
        if now is None:
            now = max(created_times) if created_times else None
        if now is None:
            return _skip(
                "no_runs",
                "No parseable run timestamps — cannot anchor the rolling window.",
                confidence=0.3, decision_path="no_anchor",
                threshold=threshold, window_hours=window_hours, window_runs=window_runs,
            )

        cutoff = now - timedelta(hours=window_hours)

        waits: list[float] = []
        for run in runs:
            if not isinstance(run, dict):
                continue
            created = _parse_iso(run.get("created_at"))
            started = _parse_iso(run.get("run_started_at"))
            if created is None or started is None:
                continue
            if created < cutoff:
                continue
            delta = (started - created).total_seconds()
            if delta < 0:
                # Clock skew / malformed pair — drop rather than trust a
                # negative wait.
                continue
            waits.append(delta)

        p90 = round(_p90(waits), 1)
        sample_size = len(waits)
        max_seconds = round(max(waits), 1) if waits else 0.0
        checked_at = now.isoformat()

        if sample_size < min_sample:
            out = _stats(
                saturated=False, reason_skipped="insufficient_sample",
                p90=p90, sample_size=sample_size, max_seconds=max_seconds,
                threshold=threshold, window_hours=window_hours,
                checked_at=checked_at, recommendation="insufficient_data",
            )
            return {
                "output": out,
                "rationale": (
                    f"Only {sample_size} valid run(s) in the {window_hours:g}h "
                    f"window (need >= {min_sample}); p90 is too noisy to trust. "
                    "Holding — no scale signal."
                ),
                "self_metric": {
                    "confidence": 0.6,
                    "decision_path": "insufficient_sample",
                    "sample_size": sample_size,
                    "p90_seconds": p90,
                },
            }

        saturated = p90 > threshold
        recommendation = "scale_runner_pool" if saturated else "hold"
        if saturated:
            rationale = (
                f"CI queue-wait p90 = {p90:.0f}s over {sample_size} run(s) in "
                f"{window_hours:g}h exceeds the {threshold:.0f}s threshold "
                f"(max {max_seconds:.0f}s) — the dfa-ci runner pool is backing "
                "up. Demand-driven signal to scale the VPS runner pool."
            )
        else:
            rationale = (
                f"CI queue-wait p90 = {p90:.0f}s over {sample_size} run(s) in "
                f"{window_hours:g}h is within the {threshold:.0f}s threshold — "
                "runners keep up, no capacity needed."
            )

        return {
            "output": _stats(
                saturated=saturated, reason_skipped=None,
                p90=p90, sample_size=sample_size, max_seconds=max_seconds,
                threshold=threshold, window_hours=window_hours,
                checked_at=checked_at, recommendation=recommendation,
            ),
            "rationale": rationale,
            "self_metric": {
                "confidence": 1.0,
                "decision_path": "evaluated",
                "sample_size": sample_size,
                "p90_seconds": p90,
            },
        }
    except Exception as e:  # noqa: BLE001 — fail-safe to the conservative verdict
        return _skip(
            "decision_error",
            f"Organ error ({e}) — failing safe to hold (no scale signal).",
            confidence=0.0, decision_path="decision_error",
            threshold=_DEFAULT_THRESHOLD_SECONDS,
            window_hours=_DEFAULT_WINDOW_HOURS,
            window_runs=_DEFAULT_WINDOW_RUNS,
        )


def _stats(*, saturated, reason_skipped, p90, sample_size, max_seconds,
           threshold, window_hours, checked_at, recommendation) -> dict:
    return {
        "saturated": saturated,
        "reason_skipped": reason_skipped,
        "p90_seconds": p90,
        "sample_size": sample_size,
        "max_seconds": max_seconds,
        "threshold_seconds": threshold,
        "window_hours": window_hours,
        "checked_at": checked_at,
        "recommendation": recommendation,
    }


def _skip(reason: str, rationale: str, *, confidence: float, decision_path: str,
          threshold: float, window_hours: float, window_runs: int = _DEFAULT_WINDOW_RUNS) -> dict:
    """Conservative no-verdict result: never saturated, low confidence."""
    return {
        "output": _stats(
            saturated=False, reason_skipped=reason,
            p90=0.0, sample_size=0, max_seconds=0.0,
            threshold=threshold, window_hours=window_hours,
            checked_at=None, recommendation="insufficient_data",
        ),
        "rationale": rationale,
        "self_metric": {
            "confidence": confidence,
            "decision_path": decision_path,
            "sample_size": 0,
            "p90_seconds": 0.0,
        },
    }


def main() -> int:
    path = os.environ.get("ORGAN_INPUT")
    raw = open(path).read() if path else sys.stdin.read()
    try:
        payload = json.loads(raw)
        state = payload["state"]
    except Exception as e:
        print(json.dumps({"error": f"invalid input: {e}"}), file=sys.stderr)
        return 1
    print(json.dumps(decide(state, payload.get("context")), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
