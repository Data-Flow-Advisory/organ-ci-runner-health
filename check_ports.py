#!/usr/bin/env python3
"""Port-manifest conformance check (the CONNECTORS.md "conformance gains a port
check" requirement) for the ci-runner-health organ.

Asserts, with no network and stdlib only:

  1. ``ports.json`` parses and is structurally valid
     (``inputs``/``outputs`` arrays of ``{name, type[, required]}``).
  2. Every ``type`` referenced by a port exists in the shared vocabulary
     (vendored ``types.json``).
  3. ``decide`` actually **reads each declared input name** under ``state``
     (static read-evidence in ``organ.py``) and every declared **required**
     input appears in at least one committed sample.
  4. ``decide`` actually **writes each declared output name** under ``output``
     — verified dynamically by shadow-running the organ on every committed
     sample and asserting the key is present in the result's ``output``.

Run standalone (exit non-zero on any failure):

    python3 check_ports.py

Importable: ``run_checks()`` returns ``(ok: bool, report: list[str])`` so the
pytest in ``test_ports.py`` can assert on the same logic.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PORTS = ROOT / "ports.json"
TYPES = ROOT / "types.json"
ORGAN = ROOT / "organ.py"
SAMPLES_DIR = ROOT / "samples"


def _load_json(path: Path):
    return json.loads(path.read_text())


def _load_organ():
    spec = importlib.util.spec_from_file_location("ci_runner_health_organ", ORGAN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_checks() -> tuple[bool, list[str]]:
    report: list[str] = []
    ok = True

    def fail(msg: str) -> None:
        nonlocal ok
        ok = False
        report.append(f"FAIL: {msg}")

    def passed(msg: str) -> None:
        report.append(f"ok:   {msg}")

    # ---- 1. ports.json parses + structure ---------------------------------- #
    try:
        ports = _load_json(PORTS)
    except Exception as e:  # noqa: BLE001
        return False, [f"FAIL: ports.json does not parse: {e}"]
    passed("ports.json parses")

    inputs = ports.get("inputs")
    outputs = ports.get("outputs")
    if not isinstance(inputs, list):
        fail("ports.json 'inputs' must be a list")
        inputs = []
    if not isinstance(outputs, list):
        fail("ports.json 'outputs' must be a list")
        outputs = []
    if not outputs:
        fail("ports.json declares no outputs — every organ writes a verdict")

    for kind, ports_list in (("input", inputs), ("output", outputs)):
        for p in ports_list:
            if not isinstance(p, dict):
                fail(f"{kind} port is not an object: {p!r}")
                continue
            if not isinstance(p.get("name"), str) or not p["name"]:
                fail(f"{kind} port missing a non-empty 'name': {p!r}")
            if not isinstance(p.get("type"), str) or not p["type"]:
                fail(f"{kind} port {p.get('name')!r} missing a non-empty 'type'")
            if kind == "input" and "required" in p and not isinstance(p["required"], bool):
                fail(f"input port {p.get('name')!r} 'required' must be bool")
    if ok:
        passed(
            f"port structure valid ({len(inputs)} inputs, {len(outputs)} outputs)"
        )

    # ---- 2. every referenced type exists in the vocabulary ----------------- #
    try:
        vocab = _load_json(TYPES)
        type_names = set((vocab.get("types") or {}).keys())
    except Exception as e:  # noqa: BLE001
        fail(f"types.json does not parse: {e}")
        type_names = set()
    else:
        passed(f"types.json parses ({len(type_names)} types in vocabulary)")

    for p in inputs + outputs:
        if isinstance(p, dict) and isinstance(p.get("type"), str):
            if p["type"] in type_names:
                passed(f"type '{p['type']}' (port '{p.get('name')}') exists in vocabulary")
            else:
                fail(
                    f"type '{p['type']}' (port '{p.get('name')}') NOT in vocabulary — "
                    "add it to types.json (and propose it upstream)"
                )

    # ---- 3. decide reads each declared input name -------------------------- #
    organ_src = ORGAN.read_text()
    for p in inputs:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        if not isinstance(name, str):
            continue
        # Static read-evidence: state.get("name") or state["name"].
        pat = re.compile(
            r"""state(?:\.get\(\s*["']%s["']|\[\s*["']%s["']\s*\])"""
            % (re.escape(name), re.escape(name))
        )
        if pat.search(organ_src):
            passed(f"decide reads declared input 'state[\"{name}\"]'")
        else:
            fail(f"decide does NOT read declared input name '{name}' under state")

    # Required inputs must be exercised by at least one committed sample.
    samples = sorted(SAMPLES_DIR.glob("*.json")) if SAMPLES_DIR.is_dir() else []
    if not samples:
        fail("no samples/*.json to validate ports against")
    sample_states = []
    for s in samples:
        try:
            payload = _load_json(s)
            sample_states.append((s.name, payload.get("state") or {}))
        except Exception as e:  # noqa: BLE001
            fail(f"sample {s.name} does not parse: {e}")
    for p in inputs:
        if isinstance(p, dict) and p.get("required") and isinstance(p.get("name"), str):
            name = p["name"]
            if any(name in st for _, st in sample_states):
                passed(f"required input '{name}' present in at least one sample")
            else:
                fail(f"required input '{name}' present in NO sample")

    # ---- 4. decide writes each declared output name ------------------------ #
    declared_out = [
        p["name"] for p in outputs
        if isinstance(p, dict) and isinstance(p.get("name"), str)
    ]
    if samples and declared_out:
        organ = _load_organ()
        for s in samples:
            try:
                payload = _load_json(s)
            except Exception:  # noqa: BLE001 — already reported above
                continue
            result = organ.decide(payload.get("state"), payload.get("context"))
            out = result.get("output")
            if not isinstance(out, dict):
                fail(f"{s.name}: decide() result has no dict 'output'")
                continue
            missing = [n for n in declared_out if n not in out]
            if missing:
                fail(f"{s.name}: output missing declared name(s): {missing}")
            else:
                passed(f"{s.name}: all {len(declared_out)} declared output names written")

    return ok, report


def main() -> int:
    ok, report = run_checks()
    print("# Port-manifest conformance — organ-ci-runner-health")
    for line in report:
        print(line)
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
