"""Phase 1 unit tests: expression language, CSM v2 loading, discovery stages."""

import itertools
import json
import sys
from pathlib import Path

import pytest

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))
sys.path.insert(0, str(WS / "discover"))

import csm_model as cm  # noqa: E402
import validate_constraints as vc  # noqa: E402
import cpp_scan  # noqa: E402
import llm_enrich  # noqa: E402

OPTS = {n: {} for n in ("A", "B", "C", "S", "N")}


# ---------------------------------------------------------------- expressions
@pytest.mark.parametrize("text", [
    "if A == 1 then B == 0",
    "not (A == 1 or B != 0) and C <= N",
    "S in [\"x\", \"y\"] or S not in [\"z\"]",
    "if A == 1 and B == 0 then C == N",
    "true", "not false",
])
def test_parse_roundtrip_and_nnf(text):
    f = cm.parse_expression(text, OPTS)
    again = cm.parse_expression(cm.condition_to_text(f), OPTS)
    nnf = cm.to_nnf(f)
    assert "\"not\":" not in json.dumps(nnf) and "\"implies\":" not in json.dumps(nnf)
    for a, b, c, s, n in itertools.product([0, 1], [0, 1], [0, 3], ["x", "z"], [0, 3]):
        cfg = {"A": a, "B": b, "C": c, "S": s, "N": n}
        assert cm.eval_condition(f, cfg) == cm.eval_condition(again, cfg) == cm.eval_condition(nnf, cfg)


def test_eval3_partial():
    f = cm.parse_expression("if A == 1 then B == 0", OPTS)
    assert cm.eval3(f, {"A": 0}) is True
    assert cm.eval3(f, {"A": 1}) is None
    assert cm.eval3(f, {"A": 1, "B": 1}) is False
    assert cm.eval3(f, {}) is None


def test_unknown_option_rejected():
    with pytest.raises(ValueError):
        cm.parse_expression("if Z == 1 then A == 1", OPTS)


def test_line_specs():
    assert cm.parse_lines("3-5,9") == [3, 4, 5, 9]
    assert cm.format_lines([9, 3, 4, 5]) == "3-5,9"


# ---------------------------------------------------------------- CSM v2
@pytest.fixture(scope="module")
def csm():
    return cm.load_csm(WS / "csm.yaml")


def test_yaml_and_json_views_agree(csm):
    j = cm.load_csm(WS / "csm.json")
    assert j.order == csm.order
    assert [j.settings(n) for n in j.order] == [csm.settings(n) for n in csm.order]
    assert [c["expression"] for c in j.all_constraints] == [c["expression"] for c in csm.all_constraints]


def test_self_test_clean(csm):
    assert vc.self_test(csm) == []


def test_partition_mapping(csm):
    assert csm.representative("INI_INITIAL_ALLOC", 12) == 5
    assert csm.representative("INI_MAX_LINE", 100) == 20
    assert csm.representative("INI_MAX_LINE", 1) is None   # below range
    rep = vc.validate(csm, {"INI_USE_STACK": 0, "INI_INITIAL_ALLOC": 12})
    assert rep["valid"] and any("maps to partition 'tiny'" in w for w in rep["warnings"])


def test_hard_soft_and_inert(csm):
    assert not vc.validate(csm, {"INI_CUSTOM_ALLOCATOR": 1})["valid"]
    r = vc.validate(csm, {"INI_USE_STACK": 0, "INI_INITIAL_ALLOC": 5, "INI_MAX_LINE": 200})
    assert r["valid"] and r["violated_constraints"][0]["id"] == "C3"
    assert not vc.validate(csm, {"INI_USE_STACK": 0, "INI_INITIAL_ALLOC": 5}, strict=True)["valid"]
    eff = vc.validate(csm, {"INI_INITIAL_ALLOC": 5})["effective_configuration"]
    assert eff["INI_INITIAL_ALLOC"] == 200


def test_space_stats_unchanged(csm):
    st = vc.space_stats(csm)
    assert st["valid_configurations"] == 35389440
    assert st["distinct_effective_builds"] == 450560


# ---------------------------------------------------------------- S1 scanner
SYNTH_H = """\
#ifndef SYN_H
#define SYN_H
/* Enable the feature. Requires OPT_B to be 1. */
#ifndef OPT_A
#define OPT_A 0
#endif
#ifndef OPT_B
#define OPT_B 1
#endif
#ifndef OPT_SIZE
#define OPT_SIZE 64
#endif
#ifndef OPT_CHARS
#define OPT_CHARS ";#"
#endif
#endif
"""
SYNTH_C = """\
#include "syn.h"
#if OPT_A && !OPT_B
#error OPT_A needs OPT_B
#endif
#if OPT_B
static char buf[OPT_SIZE];
#elif OPT_A
static int x;
#else
static int y;
#endif
int f(char c) { return strchr(OPT_CHARS, c) != 0; }
"""


def test_cpp_scan_synthetic(tmp_path):
    (tmp_path / "syn.h").write_text(SYNTH_H)
    (tmp_path / "syn.c").write_text(SYNTH_C)
    res = cpp_scan.scan(tmp_path, ["syn.h", "syn.c"])
    o = {x["name"]: x for x in res["options"]}
    assert set(o) == {"OPT_A", "OPT_B", "OPT_SIZE", "OPT_CHARS"}       # include guard SYN_H skipped
    assert o["OPT_SIZE"]["type"] == "integer" and o["OPT_CHARS"]["type"] == "string"
    assert o["OPT_A"]["type"] == "boolean" and o["OPT_A"]["default"] == 0
    assert o["OPT_SIZE"]["active_when"] == "OPT_B == 1"                 # used only under #if OPT_B
    assert o["OPT_CHARS"]["active_when"] is None                        # always active
    # OPT_A matters in the #error test only when OPT_B == 0, and in the #elif only when OPT_B == 0
    assert o["OPT_A"]["active_when"] == "OPT_B == 0"
    assert res["constraint_candidates"] and res["constraint_candidates"][0]["options"] == ["OPT_A", "OPT_B"]
    assert any(set(l["options"]) == {"OPT_A", "OPT_B"} for l in res["leads"])


def test_minimise_dnf():
    truth = {(a, b): bool(a or b) for a in (0, 1) for b in (0, 1)}
    terms = cpp_scan.minimise(["X", "Y"], truth)
    assert sorted(map(lambda t: tuple(sorted(t.items())), terms)) == [(("X", 1),), (("Y", 1),)]


# ---------------------------------------------------------------- S4 guards
def test_s4_guards_on_fixture(tmp_path):
    root = (WS.parent / "subject_system").resolve()
    if not (root / "ini.c").exists():
        pytest.skip("subject checkout missing")
    res = llm_enrich.ingest(root, WS / "csm.yaml",
                            (WS / "tests/fixtures/s4_response_fixture.yaml").read_text(), "fixture",
                            ["ini.c", "ini.h"])
    assert res["stats"]["proposed"] == 7 and res["stats"]["accepted"] == 4
    assert res["stats"]["rejected_by_guard"] == {"G2": 1, "G3": 1, "G4": 1}
