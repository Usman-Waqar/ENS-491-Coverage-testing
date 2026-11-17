"""Phase 2 unit tests: ACTS export/translation, CSV import, coverage checker, built-in generator."""

import itertools
import json
import sys
from pathlib import Path

import pytest
import yaml

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))

import acts_bridge as ab  # noqa: E402
import csm_model as cm  # noqa: E402

TINY = {
    "csm_version": "2.0",
    "system": {"name": "tiny", "repository": "x", "commit": "0", "language": "C"},
    "options": [
        {"name": "A", "binding": "compile_time", "description": "a", "domain": {"type": "boolean"},
         "default": 0, "apply": {"mechanism": "c_macro", "macro": "A"},
         "evidence": [{"file": "t.h", "lines": 1, "kind": "definition"}]},
        {"name": "B", "binding": "compile_time", "description": "b", "domain": {"type": "boolean"},
         "default": 1, "apply": {"mechanism": "c_macro", "macro": "B"},
         "evidence": [{"file": "t.h", "lines": 2, "kind": "definition"}]},
        {"name": "N", "binding": "compile_time", "description": "n",
         "domain": {"type": "integer", "range": {"min": 1, "max": 100}, "rationale": "r",
                    "partitions": [{"label": "lo", "interval": [1, 9], "representative": 4},
                                   {"label": "mid", "interval": [10, 49], "representative": 20},
                                   {"label": "hi", "interval": [50, "max"], "representative": 64}]},
         "default": 20, "active_when": "B == 1", "apply": {"mechanism": "c_macro", "macro": "N"},
         "evidence": [{"file": "t.h", "lines": 3, "kind": "definition"}]},
        {"name": "M", "binding": "compile_time", "description": "m",
         "domain": {"type": "integer", "range": {"min": 1, "max": 100}, "rationale": "r",
                    "partitions": [{"label": "lo", "interval": [1, 9], "representative": 4},
                                   {"label": "hi", "interval": [10, "max"], "representative": 64}]},
         "default": 64, "apply": {"mechanism": "c_macro", "macro": "M"},
         "evidence": [{"file": "t.h", "lines": 4, "kind": "definition"}]},
        {"name": "S", "binding": "compile_time", "description": "s",
         "domain": {"type": "string", "rationale": "r",
                    "values": [{"value": ";#", "token": "SEMI_HASH"}, {"value": ";"}, {"value": ""}]},
         "default": ";#", "apply": {"mechanism": "c_macro", "macro": "S"},
         "evidence": [{"file": "t.h", "lines": 5, "kind": "definition"}]},
        {"name": "api", "binding": "runtime", "description": "entry point",
         "domain": {"type": "enum", "values": ["f1", "f2", "f3"]}, "default": "f1",
         "apply": {"mechanism": "driver_cli", "flag": "--api"},
         "evidence": [{"file": "t.h", "lines": 6, "kind": "definition"}]},
    ],
    "constraints": [
        {"id": "H1", "kind": "hard", "expr": "if A == 1 then B == 0", "rationale": "r",
         "evidence": [{"file": "t.h", "lines": 7, "kind": "documentation"}]},
        {"id": "H2", "kind": "hard", "expr": "not (S == \"\" and api in [\"f2\", \"f3\"])", "rationale": "r",
         "evidence": [{"file": "t.h", "lines": 8, "kind": "documentation"}]},
        {"id": "S1", "kind": "soft", "expr": "N <= M", "rationale": "r",
         "evidence": [{"file": "t.h", "lines": 9, "kind": "documentation"}]},
    ],
    "interaction_groups": [{"id": "G", "options": ["A", "N", "api"], "reason": "shared_code",
                            "suggested_strength": 3}],
    "test_suite": {"drivers": [{"id": "d", "source": "x.c", "origin": "developer",
                                "fixed_runtime": {"api": "f1"}}],
                   "variants": [{"id": "v1", "driver": "d", "settings": {}, "baseline": "b"},
                                {"id": "v2", "driver": "d", "settings": {"N": 12}, "baseline": "b"}]},
}


@pytest.fixture()
def tiny(tmp_path):
    p = tmp_path / "tiny.yaml"
    p.write_text(yaml.safe_dump(TINY, sort_keys=False))
    return cm.load_csm(p)


def _acts_eval(text: str, cfg: dict, gm) -> bool:
    """Evaluate a rendered ACTS constraint in Python to check the translation."""
    expr = text.replace("=>", " IMPLIES ").replace("&&", " and ").replace("||", " or ")
    expr = expr.replace("!=", " NE ").replace(">=", " GE ").replace("<=", " LE ").replace("=", "==")
    expr = expr.replace(" NE ", "!=").replace(" GE ", ">=").replace(" LE ", "<=")
    env = {}
    for p in gm.params:
        v = cfg[p.option]
        env[p.name] = (v == 1) if p.type == "boolean" else (p.tok(v) if p.type == "enum" else v)
    env.update({"true": True, "false": False})
    if " IMPLIES " in expr:
        a, b = expr.split(" IMPLIES ", 1)
        return (not eval(a, {}, env)) or eval(b, {}, env)
    return eval(expr, {}, env)


def test_translation_semantics(tiny):
    gm = ab.GenModel(tiny, inert="free", soft="as-hard")
    for r in gm.rules:
        txt = ab.to_acts(gm, r["formula"])
        assert txt is not None and "!" not in txt.replace("!=", "")   # no negation operator
        for combo in itertools.product(*[gm.domains[n] for n in tiny.order]):
            cfg = dict(zip(tiny.order, combo))
            assert _acts_eval(txt, cfg, gm) == cm.eval_condition(r["formula"], cfg), (r["id"], txt, cfg)


def test_enum_literals_quoted_and_ints_bare(tiny):
    gm = ab.GenModel(tiny, inert="free", soft="as-hard")
    h2 = next(r for r in gm.rules if r["id"] == "H2")
    s1 = next(r for r in gm.rules if r["id"] == "S1")
    # not (S == "" and api in [f2, f3])  ->  S != "NONE" || api = "f1"  (complement on the domain)
    assert ab.to_acts(gm, h2["formula"]) == '((S != "NONE") || (api = "f1"))'
    assert ab.to_acts(gm, s1["formula"]) == "N <= M"


def test_collapse_rules_and_dedup(tiny):
    gm = ab.GenModel(tiny)
    ids = [r["id"] for r in gm.rules]
    assert "collapse:N" in ids and "S1" not in ids        # soft omitted by default
    assert gm.extend({"B": 0, "N": 4}) is None             # inert N must stay at default
    assert gm.extend({"B": 0, "N": 20}) is not None


def test_export_files_and_seeds(tiny, tmp_path):
    gm = ab.GenModel(tiny)
    meta = ab.export(gm, "developer", tmp_path / "tiny")
    txt = (tmp_path / "tiny.acts.txt").read_text()
    for sec in ("[System]", "[Parameter]", "[Relation]", "[Constraint]", "[Test Set]"):
        assert sec in txt
    assert "S (enum) : SEMI_HASH, SEMI, NONE" in txt
    assert "R2 : (A, N, api, 3)" in txt
    assert meta["acts_flags"] == {"doi": -1, "mode": "extend"}
    # v2 sets N=12, which maps onto the 'mid' class (representative 20) == default -> same row as v1
    assert len(meta["seeds"]) == 1 and meta["seeds"][0]["variants"] == ["v1", "v2"]


def test_csv_parse_and_import_with_dont_care(tiny, tmp_path):
    gm = ab.GenModel(tiny)
    meta = ab.export(gm, "none", tmp_path / "tiny")
    csv = "# Degree of interaction coverage: 2\n# Number of configurations: 2\nA,B,N,M,S,api\n" \
          "true,false,*,4,SEMI,f2\nfalse,true,64,*,NONE,f1\n"
    res = ab.import_csv(meta, csv, "cost-aware", "acts")
    assert not res["problems"]
    c1, c2 = sorted(res["configs"], key=lambda c: c["generator"]["row"])
    assert c1["configuration"]["N"] == 20 and c1["filled_dont_cares"] == ["N"]
    assert c2["configuration"]["M"] in (4, 64)
    bad = "A,B,N,M,S,api\ntrue,true,20,64,SEMI,f1\n"          # violates H1
    assert ab.import_csv(meta, bad, "cost-aware", "acts")["problems"]
    with pytest.raises(SystemExit):
        ab.import_csv(meta, "B,A,N,M,S,api\n", "cost-aware", "acts")


def test_coverage_counts_exhaustive(tiny):
    gm = ab.GenModel(tiny, relations="none")
    req, infeas = gm.required_tuples()
    all_valid = [dict(zip(tiny.order, c)) for c in itertools.product(*[gm.domains[n] for n in tiny.order])
                 if gm.valid(dict(zip(tiny.order, c)))]
    rep = ab.coverage(gm, all_valid)
    assert rep["total"]["percent"] == 100.0
    # every required pair is feasible, every infeasible pair appears in no valid configuration
    feasible = set()
    for c in all_valid:
        feasible |= gm.tuples_of(c, gm.relations[0])
    assert req["R1"] == feasible
    assert ab.coverage(gm, all_valid[:1])["total"]["percent"] < 100.0


@pytest.mark.parametrize("strategy", ["one-level", "two-level"])
def test_generator_reaches_full_coverage(tiny, strategy):
    gm = ab.GenModel(tiny)
    seeds = [{"configuration": s["configuration"]} for s in ab.developer_seeds(gm)]
    gen = ab.generate_two_level if strategy == "two-level" else ab.generate_builtin
    rows, stats = gen(gm, seeds, 3)
    assert stats["uncovered_left"] == 0
    assert all(gm.valid(r) for r in rows)
    rep = ab.coverage(gm, [s["configuration"] for s in seeds] + rows)
    assert rep["total"]["percent"] == 100.0


def test_csv_roundtrip_of_generated_rows(tiny, tmp_path):
    gm = ab.GenModel(tiny)
    meta = ab.export(gm, "developer", tmp_path / "tiny")
    rows, _ = ab.generate_builtin(gm, [], 1)
    ab.write_csv(gm, rows, tmp_path / "o.csv", ["Degree of interaction coverage: -1"])
    res = ab.import_csv(meta, (tmp_path / "o.csv").read_text(), "cost-aware", "builtin")
    assert not res["problems"] and len(res["configs"]) == len(rows)
    assert ab.coverage(gm, [c["configuration"] for c in res["configs"]])["total"]["percent"] == 100.0
