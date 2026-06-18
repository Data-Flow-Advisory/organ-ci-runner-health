# CI Runner Health Organ

A **pure decider** that judges whether the self-hosted `dfa-ci` GitHub Actions
runner pool is **saturating** — extracted from discovery-engine's
`app/services/ci_runner_health.py`.

It turns recent CI workflow runs into a demand-driven *"scale the VPS runner
pool"* signal: compute the p90 of the queue-wait (`run_started_at - created_at`)
over a rolling window, and advise saturation when that p90 exceeds a threshold —
without ever touching a database, network, or clock.

## What it does

Given the already-fetched recent `ci.yml` workflow runs, the organ:

1. **Filters** runs to those created within the rolling window (default 4h) that
   carry both timestamps and a **non-negative** queue-wait (clock-skew pairs are
   dropped, not trusted).
2. **Computes** the p90 queue-wait via the nearest-rank method
   (`rank = ceil(0.9 * n)`), plus the max and the sample size.
3. **Gates on sample size** — below `min_sample` (default 5) valid runs the p90
   is too noisy to trust, so it holds with `reason_skipped = "insufficient_sample"`.
4. **Decides** — `saturated = p90 > threshold_seconds` (default 60s). When
   saturated, the recommendation is `scale_runner_pool`; otherwise `hold`.

The impure parts stay in the **spine**: fetching the runs from the GitHub REST
API, inserting the `ci_runner_saturation` PendingWidgetAction (with dedup), and
reading thresholds from environment variables. The organ is *handed* the runs in
`state` and only *advises*.

## Input contract

```json
{
  "state": {
    "now": "2026-06-03T11:00:00Z",
    "runs": [
      {"created_at": "2026-06-03T10:00:00Z", "run_started_at": "2026-06-03T10:02:30Z"}
    ]
  },
  "context": {
    "threshold_seconds": 60,
    "window_hours": 4,
    "min_sample": 5,
    "window_runs": 50
  }
}
```

### Fields

- **state.runs** (list[dict], required): raw `ci.yml` workflow runs. Each needs a
  `created_at` (when queued) and a `run_started_at` (when a runner picked it up),
  both GitHub ISO-8601 (`...Z`).
- **state.now** (str|null): reference clock for the rolling-window cutoff.
  *Optional* — when absent the window is anchored deterministically to the
  freshest run's `created_at` (the organ never reads the wall clock).
- **context** (optional — the organ works with it absent):
  - **threshold_seconds** (default `60`): p90 queue-wait above this trips saturation.
  - **window_hours** (default `4`): only runs created within this window count.
  - **min_sample** (default `5`): below this many valid runs, hold instead.
  - **window_runs** (default `50`): informational — how many runs the spine fetched.

## Output contract

```json
{
  "output": {
    "saturated": true,
    "reason_skipped": null,
    "p90_seconds": 180.0,
    "sample_size": 10,
    "max_seconds": 195.0,
    "threshold_seconds": 60.0,
    "window_hours": 4.0,
    "checked_at": "2026-06-03T11:00:00+00:00",
    "recommendation": "scale_runner_pool"
  },
  "rationale": "CI queue-wait p90 = 180s over 10 run(s) in 4h exceeds the 60s threshold ...",
  "self_metric": {
    "confidence": 1.0,
    "decision_path": "evaluated",
    "sample_size": 10,
    "p90_seconds": 180.0
  }
}
```

`recommendation` is one of `scale_runner_pool` / `hold` / `insufficient_data`.
When there is nothing to judge, `saturated` is `false`, `reason_skipped` is one of
`no_runs`, `insufficient_sample`, `decision_error`, and confidence is low.

## Run it

```bash
# from stdin
echo '{"state": {"runs": [{"created_at":"2026-06-03T10:00:00Z","run_started_at":"2026-06-03T10:00:05Z"}]}}' | python3 organ.py

# from a sample file
ORGAN_INPUT=samples/saturated_scale_pool.json python3 organ.py
```

## Test

```bash
python -m pytest -q
```

## Ports manifest (the connection stud)

[`ports.json`](ports.json) declares this organ's typed ports per the orchestrator's
[connection standard](https://github.com/Data-Flow-Advisory/orchestrator/blob/feat/drift-gate/CONNECTORS.md):
`name` is the literal wiring address (the key `decide()` reads under `state` /
writes under `output`); `type` is a name from the shared vocabulary
([`types.json`](types.json)) — two ports connect iff their `type` matches.

- **Inputs:** `runs` (`CIRunList`, required) and `now` (`Timestamp`, optional).
- **Outputs:** the flat verdict keyed by scalar type — `saturated` (`Bool`),
  `recommendation`/`reason_skipped` (`Str`), `sample_size` (`Int`),
  the seconds/hours measures (`Number`), and `checked_at` (`Timestamp`).

`check_ports.py` is the port-conformance check the conformance Action runs (and
[`test_ports.py`](test_ports.py) gates the same logic under `pytest`): it asserts
`ports.json` parses, every referenced `type` exists in `types.json`, `decide`
reads each declared input name under `state`, and `decide` writes each declared
output name under `output` (shadow-run against the committed samples).

> `types.json` is a **vendored** snapshot of the orchestrator vocabulary plus the
> primitives this organ has to propose (`Bool`/`Int`/`Number`/`Str`/`Timestamp`)
> and the domain type `CIRunList` — the vocabulary had no scalar types, which a
> flat-scalar-output organ needs. They are marked `_proposed` pending upstream
> review into the canonical `types.json`.

## Purity guarantees

- No DB / network / filesystem / clock access in `decide()`.
- Deterministic given the same input.
- Fails safe to a conservative **hold** (never a confident-wrong "scale now") on
  malformed or empty `state`.
- Stdlib-only.

See [`CONTRACT.md`](CONTRACT.md) for the full organ interface. Conformance
(shadow-run on the committed samples + the test suite) runs in CI via
`.github/workflows/conformance.yml`.
