"""Tests for the ci-runner-health organ. Vanilla pytest, stdlib only."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ORGAN = Path(__file__).parent / "organ.py"
_spec = importlib.util.spec_from_file_location("ci_runner_health_organ", ORGAN)
organ = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(organ)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #

def _run(created: str, started: str) -> dict:
    return {"created_at": created, "run_started_at": started}


def _runs_fast(n: int, base="2026-06-03T10:00:00Z") -> list[dict]:
    """n runs all with ~5s queue-wait, well under threshold."""
    out = []
    for i in range(n):
        mm = f"{i:02d}"
        out.append(_run(f"2026-06-03T10:{mm}:00Z", f"2026-06-03T10:{mm}:05Z"))
    return out


# --------------------------------------------------------------------------- #
# conservative / skip paths                                                    #
# --------------------------------------------------------------------------- #

def test_empty_state_is_conservative_hold():
    r = organ.decide({})
    assert r["output"]["saturated"] is False
    assert r["output"]["reason_skipped"] == "no_runs"
    assert r["output"]["recommendation"] == "insufficient_data"
    assert r["self_metric"]["confidence"] <= 0.5


def test_none_state_is_conservative_hold():
    r = organ.decide(None)
    assert r["output"]["saturated"] is False
    assert r["output"]["reason_skipped"] == "no_runs"


def test_empty_runs_list_holds():
    r = organ.decide({"runs": []})
    assert r["output"]["saturated"] is False
    assert r["output"]["reason_skipped"] == "no_runs"
    assert r["self_metric"]["decision_path"] == "no_runs"


def test_runs_not_a_list_holds():
    r = organ.decide({"runs": "nope"})
    assert r["output"]["saturated"] is False
    assert r["output"]["reason_skipped"] == "no_runs"


def test_unparseable_timestamps_no_anchor():
    # runs present but no parseable created_at and no `now` -> can't anchor.
    r = organ.decide({"runs": [{"created_at": "garbage", "run_started_at": "garbage"}]})
    assert r["output"]["saturated"] is False
    assert r["output"]["reason_skipped"] == "no_runs"
    assert r["self_metric"]["decision_path"] in ("no_anchor", "no_runs")


# --------------------------------------------------------------------------- #
# insufficient sample                                                          #
# --------------------------------------------------------------------------- #

def test_below_min_sample_is_insufficient():
    # 3 valid runs, default min_sample = 5.
    r = organ.decide({"runs": _runs_fast(3), "now": "2026-06-03T10:05:00Z"})
    assert r["output"]["saturated"] is False
    assert r["output"]["reason_skipped"] == "insufficient_sample"
    assert r["output"]["recommendation"] == "insufficient_data"
    assert r["self_metric"]["decision_path"] == "insufficient_sample"
    assert r["self_metric"]["confidence"] == 0.6
    assert r["self_metric"]["sample_size"] == 3


def test_min_sample_override_via_context():
    # 3 runs but min_sample lowered to 2 -> a real verdict.
    r = organ.decide(
        {"runs": _runs_fast(3), "now": "2026-06-03T10:05:00Z"},
        {"min_sample": 2},
    )
    assert r["output"]["reason_skipped"] is None
    assert r["self_metric"]["decision_path"] == "evaluated"


# --------------------------------------------------------------------------- #
# saturated / not-saturated verdicts                                           #
# --------------------------------------------------------------------------- #

def test_fast_runs_not_saturated():
    r = organ.decide({"runs": _runs_fast(10), "now": "2026-06-03T10:30:00Z"})
    assert r["output"]["saturated"] is False
    assert r["output"]["recommendation"] == "hold"
    assert r["output"]["reason_skipped"] is None
    assert r["output"]["p90_seconds"] == 5.0
    assert r["self_metric"]["confidence"] == 1.0


def test_slow_runs_saturated():
    # 10 runs each with a 120s queue-wait, threshold 60s -> saturated.
    runs = [
        _run("2026-06-03T10:00:00Z", "2026-06-03T10:02:00Z"),
        _run("2026-06-03T10:01:00Z", "2026-06-03T10:03:00Z"),
        _run("2026-06-03T10:02:00Z", "2026-06-03T10:04:00Z"),
        _run("2026-06-03T10:03:00Z", "2026-06-03T10:05:00Z"),
        _run("2026-06-03T10:04:00Z", "2026-06-03T10:06:00Z"),
        _run("2026-06-03T10:05:00Z", "2026-06-03T10:07:00Z"),
        _run("2026-06-03T10:06:00Z", "2026-06-03T10:08:00Z"),
        _run("2026-06-03T10:07:00Z", "2026-06-03T10:09:00Z"),
        _run("2026-06-03T10:08:00Z", "2026-06-03T10:10:00Z"),
        _run("2026-06-03T10:09:00Z", "2026-06-03T10:11:00Z"),
    ]
    r = organ.decide({"runs": runs, "now": "2026-06-03T10:12:00Z"})
    assert r["output"]["saturated"] is True
    assert r["output"]["recommendation"] == "scale_runner_pool"
    assert r["output"]["p90_seconds"] == 120.0
    assert r["output"]["max_seconds"] == 120.0
    assert r["self_metric"]["confidence"] == 1.0


def test_threshold_override_flips_verdict():
    # 5s waits, but threshold lowered to 2s -> saturated.
    r = organ.decide(
        {"runs": _runs_fast(10), "now": "2026-06-03T10:30:00Z"},
        {"threshold_seconds": 2},
    )
    assert r["output"]["saturated"] is True
    assert r["output"]["threshold_seconds"] == 2.0


def test_exactly_at_threshold_not_saturated():
    # p90 == threshold is NOT saturated (strict > in source).
    runs = [_run(f"2026-06-03T10:{i:02d}:00Z", f"2026-06-03T10:{i:02d}:30Z") for i in range(10)]
    r = organ.decide(
        {"runs": runs, "now": "2026-06-03T10:30:00Z"},
        {"threshold_seconds": 30},
    )
    assert r["output"]["p90_seconds"] == 30.0
    assert r["output"]["saturated"] is False


# --------------------------------------------------------------------------- #
# windowing + robustness                                                       #
# --------------------------------------------------------------------------- #

def test_old_runs_outside_window_excluded():
    # 5 fresh fast runs + 5 ancient slow runs; window 4h drops the ancient ones.
    fresh = _runs_fast(5, base="2026-06-03T10:00:00Z")
    ancient = [
        _run("2026-06-01T01:00:00Z", "2026-06-01T01:05:00Z") for _ in range(5)
    ]
    r = organ.decide({"runs": fresh + ancient, "now": "2026-06-03T10:30:00Z"})
    # only the 5 fresh runs survive the window
    assert r["output"]["sample_size"] == 5
    assert r["output"]["saturated"] is False


def test_negative_wait_dropped():
    # clock-skew run (started before created) is dropped, not trusted.
    runs = _runs_fast(5) + [_run("2026-06-03T10:20:00Z", "2026-06-03T10:19:00Z")]
    r = organ.decide({"runs": runs, "now": "2026-06-03T10:30:00Z"})
    assert r["output"]["sample_size"] == 5  # the negative one dropped


def test_missing_run_started_at_dropped():
    runs = _runs_fast(5) + [{"created_at": "2026-06-03T10:20:00Z"}]
    r = organ.decide({"runs": runs, "now": "2026-06-03T10:30:00Z"})
    assert r["output"]["sample_size"] == 5


def test_now_defaults_to_freshest_run():
    # no `now` supplied -> anchored to the freshest created_at; all runs fresh.
    r = organ.decide({"runs": _runs_fast(10)})
    assert r["output"]["sample_size"] == 10
    assert r["output"]["checked_at"] is not None


def test_non_dict_runs_skipped():
    runs = _runs_fast(5) + ["garbage", 42, None]
    r = organ.decide({"runs": runs, "now": "2026-06-03T10:30:00Z"})
    assert r["output"]["sample_size"] == 5


def test_context_absent_uses_defaults():
    r = organ.decide({"runs": _runs_fast(10), "now": "2026-06-03T10:30:00Z"})
    assert r["output"]["threshold_seconds"] == 60.0
    assert r["output"]["window_hours"] == 4.0


def test_garbage_context_values_fall_back_to_defaults():
    r = organ.decide(
        {"runs": _runs_fast(10), "now": "2026-06-03T10:30:00Z"},
        {"threshold_seconds": "abc", "window_hours": None, "min_sample": "x"},
    )
    assert r["output"]["threshold_seconds"] == 60.0
    assert r["output"]["window_hours"] == 4.0
    # defaults applied -> a normal verdict still produced
    assert r["self_metric"]["decision_path"] == "evaluated"


# --------------------------------------------------------------------------- #
# p90 unit (nearest-rank)                                                      #
# --------------------------------------------------------------------------- #

def test_p90_nearest_rank():
    assert organ._p90([]) == 0.0
    assert organ._p90([7.0]) == 7.0
    # n=10 -> rank ceil(9.0)=9 -> 9th value (0-indexed 8)
    assert organ._p90([float(i) for i in range(1, 11)]) == 9.0
    # n=5 -> rank ceil(4.5)=5 -> 5th value
    assert organ._p90([1.0, 2.0, 3.0, 4.0, 5.0]) == 5.0


def test_parse_iso_robustness():
    assert organ._parse_iso(None) is None
    assert organ._parse_iso("") is None
    assert organ._parse_iso(123) is None
    assert organ._parse_iso("not-a-date") is None
    assert organ._parse_iso("2026-06-03T10:00:00Z") is not None


# --------------------------------------------------------------------------- #
# determinism + contract shape + CLI                                           #
# --------------------------------------------------------------------------- #

def test_deterministic():
    s = {"runs": _runs_fast(8), "now": "2026-06-03T10:30:00Z"}
    assert organ.decide(dict(s)) == organ.decide(dict(s))


def test_output_contract_shape():
    r = organ.decide({"runs": _runs_fast(10), "now": "2026-06-03T10:30:00Z"})
    assert set(r.keys()) == {"output", "rationale", "self_metric"}
    assert isinstance(r["rationale"], str) and r["rationale"]
    assert 0.0 <= r["self_metric"]["confidence"] <= 1.0
    for k in ("saturated", "reason_skipped", "p90_seconds", "sample_size",
              "max_seconds", "threshold_seconds", "window_hours",
              "checked_at", "recommendation"):
        assert k in r["output"]


def test_cli_roundtrip_stdin():
    payload = {"state": {"runs": _runs_fast(10), "now": "2026-06-03T10:30:00Z"}}
    proc = subprocess.run(
        [sys.executable, str(ORGAN)],
        input=json.dumps(payload), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["output"]["recommendation"] == "hold"


def test_cli_invalid_input_exits_nonzero():
    proc = subprocess.run(
        [sys.executable, str(ORGAN)],
        input="{not json", capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "invalid input" in proc.stderr


def test_all_samples_run_clean():
    samples = sorted((Path(__file__).parent / "samples").glob("*.json"))
    assert samples, "no samples committed"
    for s in samples:
        payload = json.loads(s.read_text())
        r = organ.decide(payload["state"], payload.get("context"))
        assert set(r.keys()) == {"output", "rationale", "self_metric"}
        assert "saturated" in r["output"]
