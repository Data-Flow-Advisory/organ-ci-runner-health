"""Bind the connection-standard ports conformance into the pytest suite.

The conformance workflow runs ``check_ports.py`` as a standalone step, but
wiring the same assertions into pytest means a ports regression turns the
test job red too (defence in depth), and lets us negative-test that the
checker actually rejects broken manifests rather than passing vacuously.

Vanilla pytest, stdlib only.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
CHECK = HERE / "check_ports.py"


def _load(name):
    return json.loads((HERE / name).read_text())


# --------------------------------------------------------------------------- #
# ports.json shape + vocabulary                                                #
# --------------------------------------------------------------------------- #

def test_ports_json_parses_and_shaped():
    ports = _load("ports.json")
    assert isinstance(ports, dict)
    assert isinstance(ports["inputs"], list) and ports["inputs"]
    assert isinstance(ports["outputs"], list) and ports["outputs"]
    for p in ports["inputs"]:
        assert set(p) >= {"name", "type", "required"}
        assert isinstance(p["required"], bool)
    for p in ports["outputs"]:
        assert set(p) >= {"name", "type"}


def test_every_port_type_in_vocabulary():
    vocab = _load("types.json")["types"]
    ports = _load("ports.json")
    for p in ports["inputs"] + ports["outputs"]:
        assert p["type"] in vocab, f"{p['name']} type {p['type']} missing from vocab"


def test_declared_inputs_match_source():
    # The declared inputs are exactly the top-level state keys decide() reads.
    ports = _load("ports.json")
    assert {p["name"] for p in ports["inputs"]} == {"runs", "now"}


def test_declared_outputs_match_source():
    ports = _load("ports.json")
    assert {p["name"] for p in ports["outputs"]} == {
        "saturated", "reason_skipped", "p90_seconds", "sample_size",
        "max_seconds", "threshold_seconds", "window_hours", "checked_at",
        "recommendation",
    }


def test_runs_is_required_now_is_optional():
    ports = _load("ports.json")
    req = {p["name"]: p["required"] for p in ports["inputs"]}
    assert req["runs"] is True
    assert req["now"] is False


# --------------------------------------------------------------------------- #
# the checker itself passes on the real manifest                               #
# --------------------------------------------------------------------------- #

def test_check_ports_passes_clean():
    proc = subprocess.run(
        [sys.executable, str(CHECK)], capture_output=True, text=True, cwd=str(HERE)
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK: ports.json conforms" in proc.stdout


# --------------------------------------------------------------------------- #
# negative tests — the checker must REJECT broken manifests                    #
# --------------------------------------------------------------------------- #

def _run_check_with(tmp_path, ports=None, types=None):
    """Copy the organ + samples into tmp_path, optionally overriding
    ports.json / types.json, then run check_ports.py there."""
    import shutil
    for f in ("organ.py", "check_ports.py"):
        shutil.copy(HERE / f, tmp_path / f)
    shutil.copytree(HERE / "samples", tmp_path / "samples")
    (tmp_path / "ports.json").write_text(
        json.dumps(ports if ports is not None else _load("ports.json"))
    )
    (tmp_path / "types.json").write_text(
        json.dumps(types if types is not None else _load("types.json"))
    )
    return subprocess.run(
        [sys.executable, str(tmp_path / "check_ports.py")],
        capture_output=True, text=True, cwd=str(tmp_path),
    )


def test_checker_rejects_undeclared_output(tmp_path):
    ports = _load("ports.json")
    ports["outputs"] = [p for p in ports["outputs"] if p["name"] != "recommendation"]
    proc = _run_check_with(tmp_path, ports=ports)
    assert proc.returncode != 0
    assert "recommendation" in proc.stderr


def test_checker_rejects_wrong_input_name(tmp_path):
    ports = _load("ports.json")
    ports["inputs"] = [{"name": "runzzz", "type": "array", "required": True},
                       {"name": "now", "type": "string", "required": False}]
    proc = _run_check_with(tmp_path, ports=ports)
    assert proc.returncode != 0
    assert "reads state keys" in proc.stderr


def test_checker_rejects_unknown_type(tmp_path):
    ports = _load("ports.json")
    ports["inputs"][0]["type"] = "not_a_real_type"
    proc = _run_check_with(tmp_path, ports=ports)
    assert proc.returncode != 0
    assert "not in types.json" in proc.stderr
