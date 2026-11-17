#!/usr/bin/env python3
"""Attribute code coverage to configuration settings, and check executed interactions.

  analyze_coverage.py [RUN_DIR] [--csm csm.json] [--t 2]

RUN_DIR defaults to the run referenced by results/summary.json. Writes RUN_DIR/analysis/:

  attribution.json / attribution.md
      For every executable line and branch outcome of the SUT: the configurations that
      executed it and the settings ALL of them share (beyond settings shared by every
      configuration in the run). One shared setting -> the code path is attributed to
      that setting; several -> to their combination (an interaction candidate). A
      'necessary-and-sufficient in this sample' flag says whether every configuration
      with those settings also executed the line. Attribution is correlational: it is
      only as good as the configuration sample (a covering array makes it meaningful).

  interactions.json / interactions.md
      For each interaction group with a code region: per t-way tuple of its options,
      whether some configuration had the tuple (configuration-level coverage) and whether
      the region was actually executed in such a configuration, overall and per test case
      (test-case-aware coverage, Yilmaz 2013): a tuple that is 'covered' by a configuration
      whose tests never reach the shared code was not really tested.

  gaps.json
      Lines never executed and branch outcomes never taken in any configuration of the run,
      with source text and enclosing function; the input to the Phase 4 gap analysis.
"""

from __future__ import annotations

import argparse
import gzip
import itertools
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import acts_bridge as ab  # noqa: E402
import csm_model as cm  # noqa: E402


def _expand(lines_by_file: dict) -> set:
    out = set()
    for f, spec in lines_by_file.items():
        for ln in cm.parse_lines(spec):
            out.add(f"{f}:{ln}")
    return out


def _source_line(subject: Path, key: str) -> str:
    f, ln = key.rsplit(":", 1)
    p = subject / f.replace("{subject}/", "")
    try:
        return p.read_text(errors="replace").splitlines()[int(ln) - 1].strip()
    except (OSError, IndexError):
        return ""


def _functions(run_dir: Path, any_cfg: str) -> list:
    p = run_dir / any_cfg / "coverage_all.gcov.json.gz"
    if not p.exists():
        return []
    data = json.load(gzip.open(p, "rt"))
    fns = []
    for d in data:
        for f in d["files"]:
            for fn in f.get("functions", []):
                fns.append((Path(f["file"]).name, fn["start_line"], fn["end_line"], fn.get("demangled_name", fn["name"])))
    return fns


def _fn_of(fns, key):
    f, ln = key.rsplit(":", 1)
    ln = int(ln)
    for name, a, b, fn in fns:
        if f.endswith(name) and a <= ln <= b:
            return fn
    return None


def attribution(gm: ab.GenModel, index: dict, subject: Path, fns) -> dict:
    cfgs = {cid: gm.normalise(c) for cid, c in index["configurations"].items() if cid in index["configs"]}
    ids = sorted(cfgs)
    covered_lines = {cid: _expand(index["configs"][cid]["lines"]) for cid in ids}
    covered_br = {cid: set(index["configs"][cid]["branches_taken"]) for cid in ids}
    universal = {(o, json.dumps(v)) for o, v in cfgs[ids[0]].items()
                 if all(cfgs[c][o] == v for c in ids)} if ids else set()

    def analyse(keys, covered):
        out = []
        for k in keys:
            who = [c for c in ids if k in covered[c]]
            entry = {"key": k, "covered_in": len(who), "of": len(ids)}
            if not who:
                entry["status"] = "never"
            elif len(who) == len(ids):
                entry["status"] = "always"
            else:
                shared = {(o, json.dumps(v)) for o, v in cfgs[who[0]].items()
                          if all(cfgs[c][o] == v for c in who)} - universal
                entry["shared_settings"] = {o: json.loads(v) for o, v in sorted(shared)}
                holders = [c for c in ids if all(json.dumps(cfgs[c][o]) == v for o, v in shared)]
                entry["necessary_and_sufficient_in_sample"] = bool(shared) and set(holders) == set(who)
                entry["status"] = ("single_setting" if len(shared) == 1 else
                                   "combination" if len(shared) > 1 else "unexplained")
            out.append(entry)
        return out

    lines = analyse(index["executable_lines"], covered_lines)
    branches = analyse(index["branches"], covered_br)
    for e in lines:
        e["source"] = _source_line(subject, e["key"])
        e["function"] = _fn_of(fns, e["key"])
    for e in branches:
        e["source"] = _source_line(subject, e["key"].rsplit(":", 1)[0])
        e["function"] = _fn_of(fns, e["key"].rsplit(":", 1)[0])
    by_setting = defaultdict(lambda: {"lines": [], "branches": []})
    for kind, coll in (("lines", lines), ("branches", branches)):
        for e in coll:
            if e["status"] == "single_setting" and e["necessary_and_sufficient_in_sample"]:
                (o, v), = e["shared_settings"].items()
                by_setting[f"{o}={json.dumps(v)}"][kind].append(e["key"])
    combos = defaultdict(lambda: {"lines": [], "branches": []})
    for kind, coll in (("lines", lines), ("branches", branches)):
        for e in coll:
            if e["status"] == "combination" and e["necessary_and_sufficient_in_sample"] and len(e["shared_settings"]) <= 3:
                k = " & ".join(f"{o}={json.dumps(v)}" for o, v in e["shared_settings"].items())
                combos[k][kind].append(e["key"])
    summary = {}
    for coll, name in ((lines, "lines"), (branches, "branches")):
        cnt = defaultdict(int)
        for e in coll:
            cnt[e["status"]] += 1
        summary[name] = dict(cnt)
    return {"configurations": len(ids), "summary": summary, "by_setting": dict(by_setting),
            "by_combination": dict(combos), "lines": lines, "branches": branches}


def interactions(gm: ab.GenModel, index: dict, run_dir: Path, per_case_path=None, raw: bool = False) -> dict:
    cfgs = {cid: gm.normalise(c) for cid, c in index["configurations"].items() if cid in index["configs"]}
    covered_lines = {cid: _expand(index["configs"][cid]["lines"]) for cid in cfgs}
    per_case = {}
    for cid in cfgs:
        p = per_case_path(cid) if per_case_path else run_dir / cid / "per_case_coverage.json"
        if p and p.exists():
            per_case[cid] = {case: _expand(v["lines"]) for case, v in json.loads(p.read_text()).items()}
    always = set.intersection(*covered_lines.values()) if covered_lines else set()
    out = {}
    for g in gm.csm.groups:
        reg = g.get("region")
        if not reg:
            continue
        rlines_all = {f"{{subject}}/{reg['file']}:{ln}" for ln in cm.parse_lines(reg["lines"])}
        # only lines that are NOT executed in every configuration discriminate between configurations
        rlines = {ln for ln in rlines_all if ln in index["executable_lines"] and ln not in always} or rlines_all
        t = min(int(g.get("suggested_strength", 2)), len(g["options"]))
        rel = {"id": g["id"], "options": [o for o in gm.csm.order if o in g["options"]], "strength": t}
        req = set()
        for subset in itertools.combinations(rel["options"], t):
            for combo in itertools.product(*[gm.domains[n] for n in subset]):
                if gm.extend(dict(zip(subset, combo))) is not None:
                    req.add(tuple((n, json.dumps(v)) for n, v in zip(subset, combo)))
        cfg_cov, exec_cov, depth, absent = set(), set(), defaultdict(int), set()
        region_exec_configs = 0
        for cid, c in cfgs.items():
            tups = gm.tuples_of(c, rel) & req
            cfg_cov |= tups
            comp = index["configs"][cid].get("lines_compiled")
            comp_set = _expand(comp) if comp is not None else None
            disc = (rlines & comp_set) if comp_set is not None else rlines
            if comp_set is not None and not (rlines_all & comp_set):
                absent |= tups      # the region has no executable code in this build
                continue
            if disc:
                executed = bool(covered_lines[cid] & disc)
            else:   # this build compiles none of the discriminating lines: any region line counts
                executed = bool(covered_lines[cid] & rlines_all)
            region_exec_configs += executed
            if executed:
                exec_cov |= tups
            for case, ls in per_case.get(cid, {}).items():
                if ls & rlines:
                    for tp in tups:
                        depth[tp] += 1
        absent -= exec_cov
        applicable = req - absent
        missing_exec = sorted((cfg_cov & applicable) - exec_cov)
        out[g["id"]] = {
            "options": rel["options"], "strength": t, "region": reg, "region_lines": len(rlines_all),
            "discriminating_region_lines": cm.format_lines(int(x.rsplit(":", 1)[1]) for x in rlines),
            "required": len(req), "configuration_covered": len(cfg_cov), "executed_covered": len(exec_cov),
            "configs_executing_region": region_exec_configs, "configs": len(cfgs),
            "percent_configuration": round(100 * len(cfg_cov) / len(req), 2) if req else None,
            "region_absent_in_build": len(absent),
            "percent_executed": round(100 * len(exec_cov) / len(applicable), 2) if applicable else None,
            "covered_but_not_executed": [{n: json.loads(v) for n, v in tp} for tp in missing_exec[:30]],
            "min_test_case_depth": min((depth.get(tp, 0) for tp in exec_cov), default=0),
            "tuples_exercised_by_one_case_only": sum(1 for tp in exec_cov if depth.get(tp, 0) == 1),
        }
        if raw:
            out[g["id"]]["_sets"] = {"required": req, "applicable": applicable, "executed": exec_cov,
                                     "configuration": cfg_cov}
    return out


def gaps(attr: dict) -> dict:
    never_lines = [e for e in attr["lines"] if e["status"] == "never"]
    never_br = [e for e in attr["branches"] if e["status"] == "never"]
    rare = [e for e in attr["lines"] if e["status"] in ("single_setting", "combination") and e["covered_in"] <= 2]
    return {"never_executed_lines": [{k: e[k] for k in ("key", "function", "source")} for e in never_lines],
            "never_taken_branches": [{k: e[k] for k in ("key", "function", "source")} for e in never_br],
            "rarely_executed_lines": [{k: e[k] for k in ("key", "function", "source", "covered_in", "shared_settings")}
                                      for e in rare]}


def to_md(attr: dict, inter: dict, gp: dict) -> tuple[str, str]:
    a = ["# Coverage attribution", "",
         f"{attr['configurations']} configurations. Status of each executable line / branch outcome:", "",
         "| | always | single setting | combination | unexplained | never |", "|---|---|---|---|---|---|"]
    for k in ("lines", "branches"):
        s = attr["summary"][k]
        a.append(f"| {k} | {s.get('always', 0)} | {s.get('single_setting', 0)} | {s.get('combination', 0)} | "
                 f"{s.get('unexplained', 0)} | {s.get('never', 0)} |")
    a += ["", "## Code paths attributed to one setting (necessary and sufficient in this sample)", "",
          "| Setting | Lines | Branch outcomes |", "|---|---|---|"]
    for k, v in sorted(attr["by_setting"].items(), key=lambda kv: -len(kv[1]["lines"]) - len(kv[1]["branches"])):
        a.append(f"| `{k}` | {cm.format_lines(int(x.rsplit(':', 1)[1]) for x in v['lines']) or '-'} | "
                 f"{len(v['branches'])} |")
    a += ["", "## Code paths attributed to a combination of settings", "",
          "| Settings | Lines | Branch outcomes |", "|---|---|---|"]
    for k, v in sorted(attr["by_combination"].items(), key=lambda kv: -len(kv[1]["lines"]) - len(kv[1]["branches"])):
        a.append(f"| `{k}` | {cm.format_lines(int(x.rsplit(':', 1)[1]) for x in v['lines']) or '-'} | "
                 f"{len(v['branches'])} |")
    a += ["", f"## Never executed in any configuration ({len(gp['never_executed_lines'])} lines, "
              f"{len(gp['never_taken_branches'])} branch outcomes)", ""]
    for e in gp["never_executed_lines"]:
        a.append(f"- line `{e['key']}` in `{e['function']}`: `{e['source']}`")
    for e in gp["never_taken_branches"]:
        a.append(f"- branch `{e['key']}` in `{e['function']}`: `{e['source']}`")
    b = ["# Executed interaction coverage (test-case-aware)", "",
         "A t-way tuple of a high-risk group is *configuration-covered* if some configuration has it, and "
         "*executed* if, in such a configuration, the tests actually ran code in the group's shared region "
         "(only region lines that are not executed in every configuration count).", "",
         "| Group | t | Required | Config-covered | Region absent in build | Executed (of applicable) | Configs reaching region | Tuples hit by one test case only |",
         "|---|---|---|---|---|---|---|---|"]
    for gid, r in inter.items():
        b.append(f"| {gid} | {r['strength']} | {r['required']} | {r['configuration_covered']} "
                 f"({r['percent_configuration']}%) | {r['region_absent_in_build']} | "
                 f"{r['executed_covered']} ({r['percent_executed']}%) | "
                 f"{r['configs_executing_region']}/{r['configs']} | {r['tuples_exercised_by_one_case_only']} |")
    return "\n".join(a) + "\n", "\n".join(b) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--csm", default=str(HERE / "csm.json"))
    ap.add_argument("--t", type=int, default=2)
    a = ap.parse_args(argv)
    if a.run_dir:
        run_dir = Path(a.run_dir)
    else:
        s = json.loads((HERE / "results" / "summary.json").read_text())
        run_dir = Path(s["run_directory"].replace("{workspace}", str(HERE)))
    csm = cm.load_csm(a.csm)
    gm = ab.GenModel(csm, a.t)
    index = json.loads((run_dir / "coverage_index.json").read_text())
    subject = (csm.path.parent / csm.data["system"]["local_path"]).resolve()
    fns = _functions(run_dir, next(iter(index["configs"])))
    attr = attribution(gm, index, subject, fns)
    inter = interactions(gm, index, run_dir)
    gp = gaps(attr)
    out = run_dir / "analysis"
    out.mkdir(exist_ok=True)
    (out / "attribution.json").write_text(json.dumps(attr, indent=1))
    (out / "interactions.json").write_text(json.dumps(inter, indent=1))
    (out / "gaps.json").write_text(json.dumps(gp, indent=1))
    ma, mb = to_md(attr, inter, gp)
    (out / "attribution.md").write_text(ma)
    (out / "interactions.md").write_text(mb)
    print(json.dumps({"run": str(run_dir), "summary": attr["summary"],
                      "settings_with_exclusive_code": len(attr["by_setting"]),
                      "combinations_with_exclusive_code": len(attr["by_combination"]),
                      "never_executed_lines": len(gp["never_executed_lines"]),
                      "never_taken_branches": len(gp["never_taken_branches"]),
                      "interactions": {k: f"cfg {v['percent_configuration']}% / executed {v['percent_executed']}%"
                                       for k, v in inter.items()}}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
