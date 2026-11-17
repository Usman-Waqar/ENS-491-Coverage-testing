"""Phase 3/4 tests: harness outputs on the default configuration; Phase 4 ingestion guards."""

import json
import os
import sys
from pathlib import Path

import pytest

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))
SUBJECT = (WS.parent / "subject_system").resolve()
pytestmark = pytest.mark.skipif(not (SUBJECT / "ini.c").exists(), reason="subject checkout missing")


def test_harness_default_outputs(tmp_path):
    import build_and_test as bt
    rc = bt.main(["--default", "--no-sanitize", "--results-dir", str(tmp_path), "--quiet"])
    assert rc == 0
    s = json.loads((tmp_path / "summary.json").read_text())
    c = s["configurations"][0]
    assert c["tests"]["failed"] == 0 and c["tests"]["crashed"] == 0
    for k in ("developer_suite", "all_tests"):
        for m in ("statement", "branch", "function"):
            assert c["coverage"][k][m]["total"] > 0
    run = Path(s["run_directory"].replace("{workspace}", str(WS)))
    if not run.exists():
        run = next((tmp_path / "runs").iterdir())
    idx = json.loads((run / "coverage_index.json").read_text())
    cov = idx["configs"]["cfg-001"]
    assert "lines_compiled" in cov and idx["configs_developer"]["cfg-001"]
    pc = json.loads((run / "cfg-001" / "per_case_coverage.json").read_text())
    assert "csm_driver/normal.ini" in pc
    assert (run / "coverage_table.md").exists() and (run / "cfg-001" / "coverage_all.table.md").exists()
    assert not (run / "builds").exists()          # cleaned up


def test_phase4_ingest_guards(tmp_path, monkeypatch):
    monkeypatch.setenv("PHASE4_ITER_ROOT", str(tmp_path))
    import importlib
    sys.path.insert(0, str(WS / "phase4"))
    import loop
    importlib.reload(loop)
    resp = tmp_path / "r.yaml"
    resp.write_text("""```yaml
tests:
  - id: ok1
    targets: ["ini.c:166"]
    configuration: {INI_MAX_LINE: 20}
    input: {name: ok1.ini, content: "[s]\\nk=v\\n"}
    expected: {differential: true}
  - id: bad_cfg
    configuration: {INI_CUSTOM_ALLOCATOR: 1}
    input: {name: b1.ini, content: "x"}
    expected: {differential: true}
  - id: bad_name
    configuration: {}
    input: {name: "../escape.ini", content: "x"}
    expected: {differential: true}
  - id: no_oracle
    configuration: {}
    input: {name: n1.ini, content: "x"}
  - id: bad_driver
    driver: unittest
    configuration: {}
    input: {name: d1.ini, content: "x"}
    expected: {differential: true}
constraint_hypotheses:
  - {id: h1, kind: soft, expr: "if INI_ALLOW_MULTILINE == 1 then INI_MAX_NAME >= 2",
     evidence: [{file: ini.c, lines: "110-111", kind: use}]}
  - {id: h2, kind: hard, expr: "if NOPE == 1 then INI_USE_STACK == 0", evidence: []}
```""")
    res = loop.ingest(7, resp, "fixture", WS / "csm.json")
    assert res["tests_accepted"] == 1
    reasons = {r["id"]: r["reason"] for r in res["rejected"]}
    assert reasons["bad_cfg"].startswith("V1") and reasons["no_oracle"].startswith("V4")
    assert reasons["bad_name"].startswith("V0") and reasons["bad_driver"].startswith("V0")
    hyp = {h[0]: h[1] for h in res["constraint_hypotheses"]}
    assert hyp == {"h1": "hypothesis", "h2": "rejected"}
    cfgs = json.loads((tmp_path / "07" / "configs.json").read_text())["configs"]
    assert cfgs[0]["extra_inputs"][0].endswith("ok1.ini")
    overlay = json.loads((tmp_path / "07" / "csm_overlay.json").read_text())
    assert "{workspace}" not in json.dumps(overlay)


def test_sanitizer_fallback(tmp_path, monkeypatch):
    """Compilers without ASan/UBSan (e.g. MinGW on Windows) must fall back to coverage-only runs."""
    import build_and_test as bt
    monkeypatch.setattr(bt, "sanitizers_available", lambda c, f: (False, "cannot find -lasan"))
    rc = bt.main(["--default", "--results-dir", str(tmp_path), "--quiet"])
    s = json.loads((tmp_path / "summary.json").read_text())
    assert rc == 0 and s["sanitizers"]["enabled"] is False and "lasan" in s["sanitizers"]["note"]
    c = s["configurations"][0]
    assert c["build"]["status"] == "success" and c["tests"]["build_failed"] == 0
    assert c["coverage"]["all_tests"]["statement"]["percent"] > 90


def test_sanitizer_probe_detects_bad_flags():
    import build_and_test as bt
    ok, why = bt.sanitizers_available("gcc", ["-fsanitize=this-does-not-exist"])
    assert not ok and why
    assert bt.sanitizers_available("gcc", ["-fsanitize=address,undefined"])[0] is True
