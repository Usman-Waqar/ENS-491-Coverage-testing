#!/usr/bin/env python3
"""Phase 4: LLM-driven test generation, constraint mining and the coverage feedback loop.

One iteration NN lives in phase4/iterations/NN/:

  prepare   --iter NN --runs RUN_DIR [RUN_DIR ...] [--dev-run RUN_DIR]
            Union the coverage of the given runs, compute the gaps (never-executed lines,
            never-taken branches, branch gaps inside high-risk regions, executed-interaction
            gaps, configuration-level t-way gaps) and write gap_report.json/.md and prompt.md
            (coverage criteria + gap tables + model summary + test contract + answer schema).

  (an LLM-based agent answers prompt.md with response.yaml in the iteration directory)

  ingest    --iter NN [--response FILE] [--model ID]
            Validate the answer: configurations against the CSM (V1), input files, oracles,
            support drivers (fault-injection shims), constraint hypotheses (evidence guards).
            Writes tests/, drivers/, configs.json, csm_overlay.json, ingest_report.json.

  execute   --iter NN       build_and_test.py on the overlay model and the new configurations.

  evaluate  --iter NN --runs RUN_DIR ... [--dev-run RUN_DIR]
            Validity of every test (V1-V5), failure-revealing tests, coverage before/after
            (statement, branch, function), executed-interaction coverage before/after, t-way
            configuration coverage before/after, constraint-hypothesis probes.
            Writes evaluation.json/.md. The iteration's run can be passed as a --runs input to
            the next iteration's prepare (feedback loop).
"""

from __future__ import annotations

import argparse
import gzip
import os
import itertools
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
WS = HERE.parent
sys.path.insert(0, str(WS))
sys.path.insert(0, str(WS / "discover"))

import yaml  # noqa: E402

import acts_bridge as ab  # noqa: E402
import analyze_coverage as ac  # noqa: E402
import csm_model as cm  # noqa: E402
import validate_constraints as vc  # noqa: E402

SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,80}\.ini$")
SAFE_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")


ITER_ROOT = Path(os.environ.get("PHASE4_ITER_ROOT", HERE / "iterations")).resolve()


def iter_dir(n: int) -> Path:
    d = ITER_ROOT / f"{n:02d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ==========================================================================
# Union of runs
# ==========================================================================

class Union:
    """Coverage of several runs taken together (keys prefixed by run index)."""

    def __init__(self, run_dirs: list[Path], developer_only: bool = False):
        self.runs = [Path(r) for r in run_dirs]
        self.index = {"configs": {}, "configurations": {}, "executable_lines": set(), "branches": set()}
        self.per_case = {}
        self.functions = []
        for i, rd in enumerate(self.runs):
            idx = json.loads((rd / "coverage_index.json").read_text())
            src = idx.get("configs_developer") if developer_only else idx["configs"]
            for cid, cov in (src or {}).items():
                key = f"r{i}:{cid}"
                self.index["configs"][key] = cov
                self.index["configurations"][key] = idx["configurations"][cid]
                self.per_case[key] = None if developer_only else rd / cid / "per_case_coverage.json"
            self.index["executable_lines"] |= set(idx["executable_lines"])
            self.index["branches"] |= set(idx["branches"])
            if not self.functions and idx["configs"]:
                self.functions = ac._functions(rd, next(iter(idx["configs"])))
        self.index["executable_lines"] = sorted(self.index["executable_lines"])
        self.index["branches"] = sorted(self.index["branches"])

    def lines(self) -> set:
        out = set()
        for c in self.index["configs"].values():
            out |= ac._expand(c["lines"])
        return out

    def branches(self) -> set:
        out = set()
        for c in self.index["configs"].values():
            out |= set(c["branches_taken"])
        return out

    def function_cov(self) -> tuple[int, int, list]:
        lines = self.lines()
        hit, missed = 0, []
        for name, a, b, fn in self.functions:
            if any(f"{{subject}}/{name}:{ln}" in lines for ln in range(a, b + 1)):
                hit += 1
            else:
                missed.append(fn)
        return hit, len(self.functions), missed

    def metrics(self) -> dict:
        L, B = self.lines(), self.branches()
        el, eb = set(self.index["executable_lines"]), set(self.index["branches"])
        fh, ft, fm = self.function_cov()
        pc = lambda a, b: round(100.0 * a / b, 2) if b else None  # noqa: E731
        return {"configurations": len(self.index["configs"]),
                "statement": {"covered": len(L & el), "total": len(el), "percent": pc(len(L & el), len(el))},
                "branch": {"covered": len(B & eb), "total": len(eb), "percent": pc(len(B & eb), len(eb))},
                "function": {"covered": fh, "total": ft, "percent": pc(fh, ft), "missed": fm}}


def _context(subject: Path, key: str, radius: int = 3) -> str:
    f, ln = key.split(":")[0], int(key.split(":")[1])
    p = subject / f.replace("{subject}/", "")
    lines = p.read_text(errors="replace").splitlines()
    a, b = max(1, ln - radius), min(len(lines), ln + radius)
    return "\n".join(f"{i:5d}{'>' if i == ln else ' '} {lines[i - 1]}" for i in range(a, b + 1))


# ==========================================================================
# prepare
# ==========================================================================

RESPONSE_SCHEMA = """\
Answer with ONE fenced ```yaml block containing:

analysis:                     # your gap analysis, most important first
  - target: "ini.c:158"       # a line "file:line", a branch outcome "file:line:index", or a group tuple
                              #   e.g. "G_reader_buffer{api_entry_point=ini_parse_string,INI_ALLOW_REALLOC=1,INI_MAX_LINE=2}"
    criterion: C1 | C2 | C3 | C4
    reachable: input_and_configuration | needs_fault_injection | unreachable
    reasoning: <why the gap exists and what reaches it>

constraint_hypotheses:        # invalid / contradictory combinations or hidden constraints you found
  - id: <short id>
    kind: hard | soft
    expr: <CSM expression, e.g. if INI_USE_STACK == 0 and INI_ALLOW_REALLOC == 1 then INI_INITIAL_ALLOC <= INI_MAX_LINE>
    rationale: <why>
    evidence: [{file: ini.c, lines: "145-161", kind: use}]

support_drivers:              # optional: fault-injection variants of an existing generated driver
  - id: <identifier>          # e.g. csm_driver_failalloc
    base: csm_driver
    source_name: <file>.c     # linked INSTEAD of the default allocator shim; must define ini_malloc,
    content: |                #   ini_free and ini_realloc (used with INI_CUSTOM_ALLOCATOR=1, INI_USE_STACK=0)
      ...C code...
    compatible_when: "INI_USE_STACK == 0 and INI_CUSTOM_ALLOCATOR == 1"

tests:
  - id: <identifier>                       # unique, [A-Za-z_][A-Za-z0-9_]*
    targets: ["ini.c:158", "ini.c:302:2"]  # what this test must execute (checked with per-test coverage)
    criterion: C1 | C2 | C3 | C4
    configuration: {INI_USE_STACK: 0, INI_ALLOW_REALLOC: 1, api_entry_point: ini_parse_string}  # others default
    driver: csm_driver                     # or a support driver id
    env: {NAME: value}                     # optional environment for the driver process
    input:
      name: <file>.ini                     # unique file name; it is also the test-case name
      content: "[s]\\nkey = value\\n"        # YAML double-quoted string; use \\r, \\t, \\xNN escapes as needed
    expected:                              # oracle: one of
      differential: true                   #   the selected entry point must agree with the reference one
      contains: ["... key=value;"]          #   substrings of the callback trace (see the driver contract)
      lines: ["... [s]", "... key=value;", "<file>.ini: e=0 user=101"]   # exact trace
"""

DRIVER_CONTRACT = """\
`csm_driver` (workspace/drivers/csm_driver.c) is the generated, runtime-configurable driver:
`csm_driver --api <api_entry_point> --handler <handler_return_policy> -- <input files>`.
For each input it prints the callback trace and a summary line (identical format to the developer
driver tests/unittest.c):

    ... [section]            <- printed when the section changes (or on a section callback)
    ... name=value;          <- one line per name/value callback (no '=value' when value is NULL)
    <file>: e=<error line or -1/-2> user=<n>

`e` is ini_parse's return value: 0 = success, >0 = first error line, -1 = open failure,
-2 = allocation failure. With INI_HANDLER_LINENO=1 every callback line ends in '  line N'.
handler_return_policy: reject_flagged = callback fails only for user=parse_error;
accept_all = never fails; reject_all = every callback fails.
The differential oracle re-parses each input through a second entry point
(ini_parse_string_length, or ini_parse when that is the selected one) and requires an identical trace
and return value. Inputs are processed in the order given; the 'user' counter starts at 100 and
increases by one per input file, so for a test's own input expect user=101 only if it is the only input:
the harness appends test inputs AFTER the 22 standard inputs, so prefer 'contains' over 'lines'
unless you account for this.
"""


def model_summary(csm: cm.CSM) -> str:
    out = []
    for n in csm.order:
        o = csm.options[n]
        aw = f"; active when {cm.condition_to_text(o['active_when'])}" if "active_when" in o else ""
        out.append(f"- `{n}` ({o['binding_time']}, default {o['default_value']!r}): settings "
                   f"{csm.settings(n)}{aw}")
    out.append("")
    for c in csm.all_constraints:
        out.append(f"- constraint {c['id']} ({c['severity']}, {c.get('status')}): `{c['expression']}`")
    for g in csm.groups:
        out.append(f"- group {g['id']} (t={g['suggested_strength']}): {g['options']}; region "
                   f"{(g.get('region') or {}).get('file')}:{(g.get('region') or {}).get('lines')}")
    return "\n".join(out)


def prepare(n: int, runs: list[Path], dev_run: Path | None, csm_path: Path) -> dict:
    d = iter_dir(n)
    csm = cm.load_csm(csm_path)
    gm = ab.GenModel(csm)
    subject = (csm.path.parent / csm.data["system"]["local_path"]).resolve()
    U = Union(runs)
    m = U.metrics()
    L, B = U.lines(), U.branches()
    never_lines = sorted(set(U.index["executable_lines"]) - L, key=lambda k: int(k.split(":")[1]))
    never_br = sorted(set(U.index["branches"]) - B, key=lambda k: (int(k.split(":")[1]), int(k.split(":")[2])))
    region_of = {}
    for g in csm.groups:
        if g.get("region"):
            for ln in cm.parse_lines(g["region"]["lines"]):
                region_of.setdefault(f"{{subject}}/{g['region']['file']}:{ln}", []).append(g["id"])
    inter = ac.interactions(gm, U.index, U.runs[0], per_case_path=lambda k: U.per_case.get(k))
    cfgs = [U.index["configurations"][k] for k in U.index["configs"]]
    tway = ab.coverage(gm, cfgs)
    builds = {}
    for k, c in U.index["configurations"].items():
        eff = gm.normalise(c)
        key = json.dumps({o: eff[o] for o in csm.compile_time_options()}, sort_keys=True)
        builds.setdefault(key, k)
    attr = ac.attribution(gm, U.index, subject, U.functions)

    # carry forward reachability classifications from earlier iterations' analyses
    classified = {}
    for prev in sorted(ITER_ROOT.glob("*/response.yaml")):
        if prev.parent == d:
            continue
        txt = prev.read_text()
        mt = re.search(r"```ya?ml\s*\n(.*?)```", txt, re.S)
        for a_ in (yaml.safe_load(mt.group(1) if mt else txt) or {}).get("analysis") or []:
            if a_.get("reachable") == "unreachable":
                classified[f"{{subject}}/{a_['target']}"] = {"iteration": prev.parent.name,
                                                            "reasoning": a_.get("reasoning")}
    open_lines = [k for k in never_lines if k not in classified]
    open_br = [k for k in never_br if k not in classified]
    inter_gaps = sum(len(r["covered_but_not_executed"]) for r in inter.values())
    converged = not open_lines and not open_br and inter_gaps == 0 and tway["total"]["covered"] == tway["total"]["required"]
    gaps = {
        "metrics": m,
        "classified_unreachable": classified,
        "open_actionable": {"lines": open_lines, "branches": open_br, "interaction_gaps": inter_gaps},
        "converged": converged,
        "never_executed_lines": [{"key": k, "function": ac._fn_of(U.functions, k), "groups": region_of.get(k, []),
                                  "context": _context(subject, k)} for k in never_lines],
        "never_taken_branches": [{"key": k, "function": ac._fn_of(U.functions, k.rsplit(':', 1)[0]),
                                  "groups": region_of.get(k.rsplit(":", 1)[0], []),
                                  "context": _context(subject, k.rsplit(":", 1)[0])} for k in never_br],
        "region_branch_gaps": [k for k in never_br if region_of.get(k.rsplit(":", 1)[0])],
        "interactions": {g: {k: v for k, v in r.items() if not k.startswith("_")} for g, r in inter.items()},
        "tway_configuration_coverage": {"total": tway["total"],
                                        "uncovered_sample": {rid: r["uncovered_sample"][:10]
                                                             for rid, r in tway["relations"].items()
                                                             if r["uncovered_count"]}},
        "existing_builds": [json.loads(k) for k in builds],
        "attribution_by_setting": {k: cm.format_lines(int(x.rsplit(":", 1)[1]) for x in v["lines"])
                                   for k, v in attr["by_setting"].items()},
        "runs": [str(r) for r in runs],
    }
    if dev_run:
        gaps["developer_suite_metrics"] = Union([dev_run], developer_only=True).metrics()
    (d / "gap_report.json").write_text(json.dumps(gaps, indent=1))

    suite = csm.data["test_suite"]
    inputs = suite.get("input_files", [])
    gen_inputs = [Path(a).name for dr in suite["drivers"] for a in dr.get("args", []) if a != "@input_files"]
    criteria = (HERE / "coverage_criteria.md").read_text()
    md = [f"# Phase 4, iteration {n:02d}: gap analysis, constraint mining and targeted test generation", "",
          "You are the test-generation agent of a combinatorial-interaction-testing study of the C library "
          "inih (`ini.c`, `ini.h`). The covering array and the existing tests below have been built and run "
          "under every listed configuration with gcov and ASan/UBSan. Analyse the remaining gaps, propose "
          "constraint hypotheses, and write targeted tests that close the gaps under the criteria.", "",
          "## Coverage criteria", "", criteria, "",
          "## Current coverage (union over all tested configurations)", "",
          f"- configurations: {m['configurations']}",
          f"- statement: {m['statement']['covered']}/{m['statement']['total']} ({m['statement']['percent']}%)",
          f"- branch outcomes: {m['branch']['covered']}/{m['branch']['total']} ({m['branch']['percent']}%)",
          f"- functions: {m['function']['covered']}/{m['function']['total']} ({m['function']['percent']}%)",
          f"- t-way configuration coverage (pairwise + 3-way groups): {tway['total']['covered']}/"
          f"{tway['total']['required']} ({tway['total']['percent']}%)"]
    if dev_run:
        dm = gaps["developer_suite_metrics"]
        md.append(f"- for comparison, the developer suite alone: statement {dm['statement']['percent']}%, "
                  f"branch {dm['branch']['percent']}%, function {dm['function']['percent']}%")
    md += ["", "## Executed interaction coverage (C1)", "",
           "| Group | t | Required | Config-covered | Executed (of applicable) | Covered but not executed (sample) |",
           "|---|---|---|---|---|---|"]
    for gid, r in inter.items():
        md.append(f"| {gid} | {r['strength']} | {r['required']} | {r['configuration_covered']} | "
                  f"{r['executed_covered']} ({r['percent_executed']}%) | {r['covered_but_not_executed'][:3]} |")
    if classified:
        md += ["", "## Gaps already classified as unreachable in earlier iterations (no new tests needed)", ""]
        for k, v in classified.items():
            if k in never_lines or k in never_br:
                md.append(f"- `{k}` (iteration {v['iteration']}): {v['reasoning']}")
    if converged:
        md += ["", "**Loop status: converged.** Every remaining gap is classified unreachable and all interaction "
               "criteria are met; further iterations should only add depth (C1 secondary) or new oracles."]
    md += ["", f"## Never-taken branch outcomes ({len(never_br)}; open: {len(open_br)}); those inside high-risk regions are C2 targets", ""]
    for e in gaps["never_taken_branches"]:
        if e["key"] in classified:
            continue
        md += [f"### `{e['key']}` in `{e['function']}`" + (f" (regions: {', '.join(e['groups'])})" if e["groups"] else ""),
               "```c", e["context"], "```"]
    md += ["", f"## Never-executed lines ({len(never_lines)}; open: {len(open_lines)})", ""]
    for e in gaps["never_executed_lines"]:
        if e["key"] in classified:
            continue
        md += [f"### `{e['key']}` in `{e['function']}`" + (f" (regions: {', '.join(e['groups'])})" if e["groups"] else ""),
               "```c", e["context"], "```"]
    md += ["", "Gcov numbers branch outcomes per line in source order (index 0 = first outcome gcov lists for "
           "that line); for `if (a && b)` the outcomes alternate true/false per condition.", "",
           "## Which settings execute which code (attribution from the runs)", ""]
    for k, v in gaps["attribution_by_setting"].items():
        md.append(f"- `{k}` -> lines {v}")
    md += ["", "## Configuration model", "", model_summary(csm), "",
           f"## Existing builds (compile-time configurations already in the runs; reuse them for C5): {len(builds)}", "",
           "```json", json.dumps(gaps["existing_builds"][:12], indent=None)[:4000], "```", "",
           "## Existing test inputs", "", f"- developer inputs (tests/): {', '.join(inputs)}",
           f"- generated inputs: {', '.join(gen_inputs)}", "",
           "## Test contract", "", DRIVER_CONTRACT, "",
           "## Source of the system under test", "", "```c", (subject / "ini.c").read_text(), "```", "",
           "## Answer format", "", RESPONSE_SCHEMA]
    (d / "prompt.md").write_text("\n".join(md))
    (d / "gap_report.md").write_text("\n".join(md[: md.index("## Configuration model")]))
    return {"iteration": n, "prompt": str(d / "prompt.md"), "metrics": m, "converged": converged,
            "open_actionable": gaps["open_actionable"], "classified_unreachable": len(classified),
            "never_lines": len(never_lines), "never_branches": len(never_br),
            "region_branch_gaps": len(gaps["region_branch_gaps"])}


# ==========================================================================
# ingest
# ==========================================================================

def _decode_content(s: str) -> bytes:
    if not isinstance(s, str):
        raise ValueError("input.content must be a string")
    return s.encode("latin-1", errors="strict")


def ingest(n: int, response_path: Path, model: str, csm_path: Path) -> dict:
    d = iter_dir(n)
    csm = cm.load_csm(csm_path)
    raw_text = response_path.read_text()
    mt = re.search(r"```ya?ml\s*\n(.*?)```", raw_text, re.S)
    resp = yaml.safe_load(mt.group(1) if mt else raw_text) or {}
    tests_dir, drv_dir = d / "tests", d / "drivers"
    tests_dir.mkdir(exist_ok=True)
    drv_dir.mkdir(exist_ok=True)
    report = {"iteration": n, "model": model, "date": str(date.today()), "tests": [], "support_drivers": [],
              "constraint_hypotheses": [], "rejected": [], "analysis_items": len(resp.get("analysis") or [])}

    overlay = json.loads(json.dumps(cm.read_model_file(csm_path)))
    ws = str(WS)
    overlay = json.loads(json.dumps(overlay).replace("{workspace}", ws))
    overlay["system"]["local_path"] = str((WS / overlay["system"]["local_path"]).resolve())
    drivers = {dr["id"]: dr for dr in overlay["test_suite"]["drivers"]}

    for sd in resp.get("support_drivers") or []:
        sid, base = sd.get("id"), sd.get("base")
        if not sid or not SAFE_ID.match(str(sid)) or sid in drivers:
            report["rejected"].append({"kind": "support_driver", "id": sid, "reason": "invalid or duplicate id"})
            continue
        if base not in drivers or drivers[base]["origin"] != "generated":
            report["rejected"].append({"kind": "support_driver", "id": sid, "reason": f"unknown base {base}"})
            continue
        name = sd.get("source_name") or f"{sid}.c"
        if not re.match(r"^[A-Za-z0-9_\-]+\.c$", name):
            report["rejected"].append({"kind": "support_driver", "id": sid, "reason": "bad source_name"})
            continue
        (drv_dir / name).write_text(sd.get("content", ""))
        nd = json.loads(json.dumps(drivers[base]))
        nd.update({"id": sid, "origin": "generated", "args": [],
                   "notes": f"LLM support driver (iteration {n:02d}, {model}) based on {base}",
                   "extra_link_sources_when": [{"when": "true", "sources": [str(drv_dir / name)]}]})
        nd.pop("baseline_proxy_for", None)
        nd.pop("baseline_valid_when", None)
        if sd.get("compatible_when"):
            try:
                cm.parse_expression(sd["compatible_when"], csm.options)
                nd["compatible_when"] = sd["compatible_when"]
            except ValueError as e:
                report["rejected"].append({"kind": "support_driver", "id": sid, "reason": f"compatible_when: {e}"})
                continue
        drivers[sid] = nd
        overlay["test_suite"]["drivers"].append(nd)
        report["support_drivers"].append({"id": sid, "base": base, "source": str(drv_dir / name)})

    groups: dict = {}
    seen_names = set()
    for t in resp.get("tests") or []:
        tid = t.get("id")
        rej = lambda why: report["rejected"].append({"kind": "test", "id": tid, "reason": why})  # noqa: E731
        if not tid or not SAFE_ID.match(str(tid)):
            rej("V0: invalid id")
            continue
        cfg = t.get("configuration") or {}
        rep = vc.validate(csm, cfg)
        if not rep["valid"]:
            rej(f"V1: invalid configuration: {rep['errors']}")
            continue
        drv = t.get("driver", "csm_driver")
        if drv not in drivers or drivers[drv]["origin"] != "generated" or not drivers[drv].get("accepts_runtime_options"):
            rej(f"V0: unknown or unsuitable driver {drv}")
            continue
        if "compatible_when" in drivers[drv] and not cm.eval_condition(
                cm.as_condition(drivers[drv]["compatible_when"], csm.options), rep["configuration"]):
            rej(f"V1: driver {drv} is not compatible with the configuration")
            continue
        inp = t.get("input") or {}
        name = str(inp.get("name", ""))
        if not SAFE_NAME.match(name) or name in seen_names:
            rej(f"V0: bad or duplicate input name {name!r}")
            continue
        try:
            data = _decode_content(inp.get("content"))
        except (ValueError, UnicodeEncodeError) as e:
            rej(f"V0: input content: {e}")
            continue
        if not data or len(data) > 65536:
            rej("V0: empty or oversized input")
            continue
        exp = t.get("expected") or {}
        if not any(k in exp for k in ("differential", "contains", "lines")):
            rej("V4: no oracle")
            continue
        seen_names.add(name)
        (tests_dir / name).write_bytes(data)
        env = {str(k): str(v) for k, v in (t.get("env") or {}).items()}
        key = json.dumps({"cfg": rep["configuration"], "env": env}, sort_keys=True)
        g = groups.setdefault(key, {"configuration": cfg, "env": env, "inputs": [], "expectations": {},
                                    "tests": []})
        g["inputs"].append(str(tests_dir / name))
        e2 = {"driver": drv}
        if "contains" in exp:
            e2["contains"] = [str(x) for x in exp["contains"]]
        if "lines" in exp:
            e2["lines"] = [str(x) for x in exp["lines"]]
        if "contains" in e2 or "lines" in e2:
            g["expectations"][name] = e2
        targets = [str(x) for x in (t.get("targets") or [])]
        rec = {"id": tid, "input": name, "driver": drv, "targets": targets, "criterion": t.get("criterion"),
               "configuration": cfg, "env": env, "oracle": sorted(k for k in exp if k in ("differential", "contains", "lines")),
               "soft_violations": [v["id"] for v in rep["violated_constraints"]],
               "warnings": rep["warnings"]}
        g["tests"].append(tid)
        report["tests"].append(rec)

    configs = []
    for i, g in enumerate(groups.values(), 1):
        configs.append({"config_id": f"llm{n:02d}-{i:02d}", "source": "llm", "tests": g["tests"],
                        "configuration": g["configuration"], "extra_inputs": g["inputs"],
                        "expectations": g["expectations"], "env": g["env"]})
    for rec in report["tests"]:
        rec["config_id"] = next(c["config_id"] for c in configs if rec["id"] in c["tests"])

    guard = None
    try:
        import llm_enrich
        subject = Path(overlay["system"]["local_path"])
        guard = llm_enrich.Guard(subject, csm, ["ini.c", "ini.h"])
    except Exception:  # noqa: BLE001
        pass
    for h in resp.get("constraint_hypotheses") or []:
        rec = {"id": h.get("id"), "kind": h.get("kind"), "expr": h.get("expr"), "rationale": h.get("rationale"),
               "evidence": h.get("evidence")}
        try:
            f = cm.parse_expression(str(h.get("expr")), csm.options)
            rec["parsed"] = True
            if h.get("kind") not in ("hard", "soft"):
                raise ValueError("kind must be hard or soft")
            if guard is not None:
                why = guard.evidence(h.get("evidence"), cm.referenced_options(f))
                rec["evidence_check"] = why or "ok"
            rec["status"] = "hypothesis"
        except ValueError as e:
            rec["parsed"] = False
            rec["status"] = "rejected"
            rec["reason"] = str(e)
        report["constraint_hypotheses"].append(rec)

    (d / "configs.json").write_text(json.dumps({"configs": configs}, indent=1))
    (d / "csm_overlay.json").write_text(json.dumps(overlay, indent=1))
    (d / "ingest_report.json").write_text(json.dumps(report, indent=1))
    return {"tests_accepted": len(report["tests"]), "configs": len(configs),
            "support_drivers": len(report["support_drivers"]), "rejected": report["rejected"],
            "constraint_hypotheses": [(h["id"], h["status"], h.get("evidence_check")) for h in report["constraint_hypotheses"]]}


# ==========================================================================
# execute
# ==========================================================================

def execute(n: int) -> int:
    d = iter_dir(n)
    cmd = [sys.executable, str(WS / "build_and_test.py"), "--csm", str(d / "csm_overlay.json"),
           "--configs", str(d / "configs.json"), "--results-dir", str(d / "results"),
           "--label", f"phase4 iteration {n:02d}", "--quiet"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    (d / "execute.log").write_text(r.stdout + r.stderr)
    return r.returncode


# ==========================================================================
# evaluate
# ==========================================================================

def _target_hit(pc: dict | None, target: str) -> bool | None:
    if pc is None:
        return None
    parts = target.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    f = parts[0]
    key_file = f"{{subject}}/{f}"
    if len(parts) == 2:
        return int(parts[1]) in set(cm.parse_lines(pc["lines"].get(key_file, "")))
    return f"{key_file}:{parts[1]}:{parts[2]}" in set(pc["branches_taken"])


def evaluate(n: int, runs: list[Path], dev_run: Path | None, csm_path: Path) -> dict:
    d = iter_dir(n)
    csm = cm.load_csm(csm_path)
    gm = ab.GenModel(csm)
    ing = json.loads((d / "ingest_report.json").read_text())
    summ = json.loads((d / "results" / "summary.json").read_text())
    run_dir = Path(summ["run_directory"].replace("{workspace}", str(d)))
    if not run_dir.exists():
        run_dir = sorted((d / "results" / "runs").iterdir())[-1]
    by_cfg = {c["config_id"]: c for c in summ["configurations"]}
    rejected_cfg = {r["config_id"] for r in summ.get("rejected_configurations", [])}

    tests = []
    for t in ing["tests"]:
        cid = t["config_id"]
        res = by_cfg.get(cid)
        row = {"id": t["id"], "config_id": cid, "driver": t["driver"], "input": t["input"],
               "criterion": t.get("criterion"), "targets": t["targets"], "V1": cid not in rejected_cfg}
        if res is None or res.get("tests") is None:
            row.update(V2=False, valid=False, reason="not built / not run")
            tests.append(row)
            continue
        row["V2"] = res["build"]["status"] == "success" and \
            res["build"]["drivers"].get(t["driver"], {}).get("status") == "success"
        case = next((c for c in res["tests"]["cases"] if c["driver"] == t["driver"] and c["case"] == t["input"]), None)
        crash = next((p["crash"] for p in res["tests"]["crashes"] if p["driver"] == t["driver"]), None)
        row["V3"] = case is not None and case["status"] not in ("not_run", "crash")
        row["oracles"] = case["oracles"] if case else {}
        row["V4"] = bool(row["oracles"])
        pcf = run_dir / cid / "per_case_coverage.json"
        pc = json.loads(pcf.read_text()).get(f"{t['driver']}/{t['input']}") if pcf.exists() else None
        hits = {tg: _target_hit(pc, tg) for tg in t["targets"]}
        row["target_hits"] = hits
        row["V5"] = bool(hits) and all(v is True for v in hits.values())
        verdicts = set(row["oracles"].values())
        row["oracle_failed"] = bool(verdicts & {"fail", "missing_output", "unexpected_case"})
        row["sanitizer_or_crash"] = crash
        row["valid"] = all(row.get(k) for k in ("V1", "V2", "V3", "V4", "V5"))
        row["failure_revealing"] = row["valid"] and (row["oracle_failed"] or bool(crash))
        if case and case.get("diff"):
            row["diff"] = case["diff"][:12]
        if case and case.get("expected_missing"):
            row["expected_missing"] = case["expected_missing"]
        tests.append(row)

    before = Union(runs)
    after = Union(runs + [run_dir])
    mb, ma = before.metrics(), after.metrics()
    new_lines = sorted(after.lines() - before.lines())
    new_br = sorted(after.branches() - before.branches())
    ib = ac.interactions(gm, before.index, before.runs[0], per_case_path=lambda k: before.per_case.get(k), raw=True)
    ia = ac.interactions(gm, after.index, after.runs[0], per_case_path=lambda k: after.per_case.get(k), raw=True)
    region_lines = set()
    for g in csm.groups:
        if g.get("region"):
            region_lines |= {f"{{subject}}/{g['region']['file']}:{ln}" for ln in cm.parse_lines(g["region"]["lines"])}
    def region_gaps(U):
        never = set(U.index["branches"]) - U.branches()
        return sorted(k for k in never if k.rsplit(":", 1)[0] in region_lines)
    cb = ab.coverage(gm, [before.index["configurations"][k] for k in before.index["configs"]])
    ca = ab.coverage(gm, [after.index["configurations"][k] for k in after.index["configs"]])
    dev = Union([dev_run], developer_only=True).metrics() if dev_run else None

    # constraint hypotheses: probe hard ones by building and running a violating configuration
    probes = {}
    hyps = [h for h in ing["constraint_hypotheses"] if h.get("parsed")]
    if hyps:
        import verify
        view = verify.model_view_from_csm(csm)
        view["constraints"] = [{"id": h["id"], "kind": h["kind"], "expr": h["expr"]} for h in hyps]
        harness = {"build": csm.data["build"], "test_suite": csm.raw["test_suite"]}
        subject = (csm.path.parent / csm.data["system"]["local_path"]).resolve()
        prober = verify.Prober(subject, harness, csm.path.parent)
        probes = verify.check_constraints(view, prober)
        for h in hyps:
            if h["kind"] == "soft":
                probes[h["id"]] = {"status": "documentary_only",
                                   "note": "soft constraints are legal by definition; kept as hypothesis"}
        # compare with the model: is the hypothesis already implied by / equivalent to a CSM constraint?
        for h in hyps:
            f = cm.parse_expression(h["expr"], csm.options)
            names = sorted(cm.referenced_options(f))
            eq = []
            for c in csm.all_constraints:
                g_ = csm.formula(c["id"])
                nn = sorted(set(names) | cm.referenced_options(g_))
                same = True
                for combo in itertools.product(*[csm.settings(x) for x in nn]):
                    cfg = dict(csm.defaults(), **dict(zip(nn, combo)))
                    if cm.eval_condition(f, cfg) != cm.eval_condition(g_, cfg):
                        same = False
                        break
                if same:
                    eq.append(c["id"])
            probes.setdefault(h["id"], {})["equivalent_to_existing"] = eq

    valid = [t for t in tests if t["valid"]]
    targets_closed = sorted({tg for t in valid for tg, hit in t["target_hits"].items() if hit})
    ev = {
        "iteration": n, "model": ing["model"], "date": str(date.today()),
        "tests": {"proposed": len(ing["tests"]) + sum(1 for r in ing["rejected"] if r["kind"] == "test"),
                  "accepted_at_ingest": len(ing["tests"]), "valid": len(valid),
                  "validity_rate": round(len(valid) / max(1, len(ing["tests"]) + sum(1 for r in ing["rejected"] if r["kind"] == "test")), 3),
                  "failure_revealing": [t["id"] for t in tests if t.get("failure_revealing")],
                  "per_test": tests, "rejected_at_ingest": ing["rejected"]},
        "targets_closed": targets_closed,
        "coverage": {"developer_suite": dev, "before": mb, "after": ma,
                     "newly_executed_lines": new_lines, "newly_taken_branches": new_br},
        "interactions": {g: {"before": ib[g]["percent_executed"], "after": ia[g]["percent_executed"],
                             "applicable": len(ia[g]["_sets"]["applicable"]),
                             "single_case_tuples_before": ib[g]["tuples_exercised_by_one_case_only"],
                             "single_case_tuples_after": ia[g]["tuples_exercised_by_one_case_only"]}
                         for g in ia},
        "region_branch_gaps": {"before": region_gaps(before), "after": region_gaps(after)},
        "tway_configuration_coverage": {"before": cb["total"], "after": ca["total"]},
        "builds": {"new_configurations": len(by_cfg), "builds": summ["aggregate"]["builds"]},
        "constraint_hypotheses": [{**h, "probe": probes.get(h["id"])} for h in ing["constraint_hypotheses"]],
        "run_dir": str(run_dir),
    }
    (d / "evaluation.json").write_text(json.dumps(ev, indent=1, default=list))
    (d / "evaluation.md").write_text(eval_md(ev))
    return ev


def eval_md(ev: dict) -> str:
    c = ev["coverage"]
    row = lambda name, m: (f"| {name} | {m['configurations']} | {m['statement']['covered']}/{m['statement']['total']} "  # noqa: E731
                           f"({m['statement']['percent']}%) | {m['branch']['covered']}/{m['branch']['total']} "
                           f"({m['branch']['percent']}%) | {m['function']['covered']}/{m['function']['total']} "
                           f"({m['function']['percent']}%) |")
    t = ev["tests"]
    out = [f"# Phase 4 iteration {ev['iteration']:02d}: evaluation", "",
           f"Test generator: {ev['model']}. Date: {ev['date']}.", "",
           "## Coverage of `ini.c` (union over configurations)", "",
           "| Test set | Configurations | Statement | Branch outcomes | Functions |", "|---|---|---|---|---|"]
    if c["developer_suite"]:
        out.append(row("Developer suite (16 variants, developer drivers)", c["developer_suite"]))
    out += [row("Before: covering array + existing tests", c["before"]),
            row("After: + LLM tests of this iteration", c["after"]), "",
            f"Newly executed lines: {', '.join(k.split(':', 1)[1] for k in c['newly_executed_lines']) or 'none'}  ",
            f"Newly taken branch outcomes: {', '.join(k.split(':', 1)[1] for k in c['newly_taken_branches']) or 'none'}",
            "", "## Generated tests", "",
            f"Proposed {t['proposed']}, accepted at ingestion {t['accepted_at_ingest']}, valid {t['valid']} "
            f"(validity rate {t['validity_rate']}). Failure-revealing: {t['failure_revealing'] or 'none'}.", "",
            "| Test | Config | Driver | Criterion | V1 | V2 | V3 | V4 | V5 targets hit | Oracles | Valid |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    yn = lambda b: "yes" if b else ("**no**" if b is False else "-")  # noqa: E731
    for r in t["per_test"]:
        hits = ", ".join(f"{k.split(':', 1)[1]}{'' if v else ' (MISSED)'}" for k, v in r.get("target_hits", {}).items())
        out.append(f"| {r['id']} | {r['config_id']} | {r['driver']} | {r.get('criterion')} | {yn(r.get('V1'))} | "
                   f"{yn(r.get('V2'))} | {yn(r.get('V3'))} | {yn(r.get('V4'))} | {yn(r.get('V5'))} {hits} | "
                   f"{r.get('oracles', {})} | {yn(r.get('valid'))} |")
    for r in t["rejected_at_ingest"]:
        out.append(f"| {r.get('id')} | - | - | - | rejected at ingestion: {r['reason']} | | | | | | **no** |")
    rg = ev["region_branch_gaps"]
    out += ["", "## Branch outcomes never taken inside high-risk regions (C2)", "",
            f"Before: {len(rg['before'])} ({', '.join(k.split(':', 1)[1] for k in rg['before'])})  ",
            f"After: {len(rg['after'])} ({', '.join(k.split(':', 1)[1] for k in rg['after']) or 'none'})",
            "", "## Executed interaction coverage (C1)", "",
            "| Group | Applicable tuples | Before | After |", "|---|---|---|---|"]
    for g, v in ev["interactions"].items():
        out.append(f"| {g} | {v['applicable']} | {v['before']}% | {v['after']}% |")
    tw = ev["tway_configuration_coverage"]
    out += ["", f"t-way configuration coverage: {tw['before']['covered']}/{tw['before']['required']} -> "
                f"{tw['after']['covered']}/{tw['after']['required']}", "",
            "## Constraint hypotheses", "", "| Id | Kind | Expression | Evidence check | Probe | Equivalent to |",
            "|---|---|---|---|---|---|"]
    for h in ev["constraint_hypotheses"]:
        p = h.get("probe") or {}
        out.append(f"| {h['id']} | {h['kind']} | `{h['expr']}` | {h.get('evidence_check', h.get('reason'))} | "
                   f"{p.get('status')} {p.get('outcome', '') or ''} | {', '.join(p.get('equivalent_to_existing', [])) or '-'} |")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("cmd", choices=["prepare", "ingest", "execute", "evaluate"])
    ap.add_argument("--iter", type=int, required=True)
    ap.add_argument("--runs", nargs="*", default=[])
    ap.add_argument("--dev-run")
    ap.add_argument("--response")
    ap.add_argument("--model", default="unknown")
    ap.add_argument("--csm", default=str(WS / "csm.json"))
    a = ap.parse_args(argv)
    if a.cmd == "prepare":
        print(json.dumps(prepare(a.iter, [Path(r) for r in a.runs], Path(a.dev_run) if a.dev_run else None,
                                 Path(a.csm)), indent=2))
    elif a.cmd == "ingest":
        resp = Path(a.response) if a.response else iter_dir(a.iter) / "response.yaml"
        print(json.dumps(ingest(a.iter, resp, a.model, Path(a.csm)), indent=2))
    elif a.cmd == "execute":
        rc = execute(a.iter)
        print(f"build_and_test exit {rc}; log in {iter_dir(a.iter) / 'execute.log'}")
    else:
        ev = evaluate(a.iter, [Path(r) for r in a.runs], Path(a.dev_run) if a.dev_run else None, Path(a.csm))
        print(json.dumps({"tests": {k: v for k, v in ev["tests"].items() if k != "per_test"},
                          "targets_closed": ev["targets_closed"],
                          "before": {k: ev["coverage"]["before"][k]["percent"] for k in ("statement", "branch", "function")},
                          "after": {k: ev["coverage"]["after"][k]["percent"] for k in ("statement", "branch", "function")},
                          "interactions": ev["interactions"],
                          "hypotheses": [(h["id"], (h.get("probe") or {}).get("status")) for h in ev["constraint_hypotheses"]]},
                         indent=2, default=list))
    return 0


if __name__ == "__main__":
    sys.exit(main())
