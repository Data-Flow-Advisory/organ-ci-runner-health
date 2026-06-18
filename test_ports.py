"""Tests for the ci-runner-health organ's typed-ports manifest (CONNECTORS.md
"conformance gains a port check"). Vanilla pytest, stdlib only.

These assert the same four properties ``check_ports.py`` enforces in CI, plus a
couple of finer-grained guards, so a local ``pytest`` run gates the manifest the
same way the conformance Action does.
"""
import importlib.util
import json
from pathlib import Path

import check_ports

ROOT = Path(__file__).parent


def _load(name):
    return json.loads((ROOT / name).read_text())


def test_check_ports_passes():
    ok, report = check_ports.run_checks()
    assert ok, "port-conformance failed:\n" + "\n".join(report)


def test_ports_json_structure():
    ports = _load("ports.json")
    assert isinstance(ports["inputs"], list) and ports["inputs"]
    assert isinstance(ports["outputs"], list) and ports["outputs"]
    for p in ports["inputs"]:
        assert set(p) >= {"name", "type"}
        assert isinstance(p["name"], str) and p["name"]
        assert isinstance(p["type"], str) and p["type"]
    for p in ports["outputs"]:
        assert set(p) >= {"name", "type"}
        assert isinstance(p["name"], str) and p["name"]
        assert isinstance(p["type"], str) and p["type"]


def test_every_port_type_in_vocabulary():
    ports = _load("ports.json")
    vocab = set(_load("types.json")["types"].keys())
    for p in ports["inputs"] + ports["outputs"]:
        assert p["type"] in vocab, f"type {p['type']} missing from types.json"


def test_declared_inputs_match_decide_reads():
    """Every declared input name is one decide() actually reads under state."""
    ports = _load("ports.json")
    src = (ROOT / "organ.py").read_text()
    for p in ports["inputs"]:
        name = p["name"]
        assert (f'state.get("{name}"' in src) or (f'state["{name}"]' in src), \
            f"organ.py does not read state['{name}']"


def test_declared_outputs_written_on_every_sample():
    """Every declared output name appears under output on every sample run."""
    ports = _load("ports.json")
    declared = [p["name"] for p in ports["outputs"]]
    spec = importlib.util.spec_from_file_location("organ_mod", ROOT / "organ.py")
    organ = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(organ)
    for s in sorted((ROOT / "samples").glob("*.json")):
        payload = json.loads(s.read_text())
        out = organ.decide(payload.get("state"), payload.get("context"))["output"]
        for name in declared:
            assert name in out, f"{s.name}: output missing '{name}'"


def test_input_names_are_top_level_state_keys_not_nested():
    """Guard: 'created_at'/'run_started_at' live inside CIRunList, not as ports."""
    ports = _load("ports.json")
    input_names = {p["name"] for p in ports["inputs"]}
    assert input_names == {"runs", "now"}
    assert "created_at" not in input_names
    assert "run_started_at" not in input_names
