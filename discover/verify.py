#!/usr/bin/env python3
"""S5 - empirical confirmation / refutation of discovered model elements.

Checks
  inertness   For an option with an `active_when` candidate: in every context
              (assignment of the condition's boolean options) where the condition
              is false, changing the option must leave `gcc -E -P` output
              byte-identical; in at least one context where it is true, it must
              change it. Options without a candidate are checked one-at-a-time:
              flipping each other boolean option from its default must not make
              the option inert (otherwise an activation condition was missed).
  bounds      For integer options: compile ini.c and the developer driver with
              ASan/UBSan at small values (0..3) in a context where the option is
              active, run the developer suite, and report the smallest value from
              which every probed value is clean.
  constraints For each hard constraint: build and run a violating configuration;
              a build/link failure, sanitizer report or crash confirms it
              empirically; a clean run leaves only documentary support.

Input is a 'model view' JSON: {options: {name: {type, default, settings,
active_when}}, constraints: [{id, kind, expr}]}, produced by merge.py from
S1-S4 output or by `model_view_from_csm()` for an existing CSM.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import csm_model as cm  # noqa: E402

SAN = ["-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-g", "-O0"]
SAN_RE = re.compile(r"ERROR: AddressSanitizer|runtime error:|ERROR: LeakSanitizer")


def render_define(name: str, value, typ: str) -> str:
    if typ == "string":
        return f'-D{name}="' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"-D{name}={int(value)}"


def alternatives(o: dict) -> list:
    if o.get("settings"):
        return [v for v in o["settings"] if v != o["default"]][:3]
    t = o["type"]
    if t == "boolean":
        return [1 - o["default"]]
    if t == "integer":
        return [o["default"] + 7, max(1, o["default"] // 2)]
    return ["", "#"] if o["default"] != "#" else ["", ";"]


class Preprocessor:
    def __init__(self, root: Path, source: str, include_dirs: list[str]):
        self.root, self.source = root, source
        self.incs = [f"-I{root / d}" for d in include_dirs] or [f"-I{root}"]
        self.cache = {}

    def digest(self, defines: tuple) -> str:
        if defines in self.cache:
            return self.cache[defines]
        r = subprocess.run(["gcc", "-E", "-P", *self.incs, *defines, str(self.root / self.source)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            h = "ERROR:" + r.stderr.strip().splitlines()[-1][:120] if r.stderr.strip() else "ERROR"
        else:
            h = hashlib.sha1(r.stdout.encode()).hexdigest()
        self.cache[defines] = h
        return h


def _defines(view: dict, assign: dict) -> tuple:
    return tuple(sorted(render_define(n, v, view["options"][n]["type"]) for n, v in assign.items()))


def check_inertness(view: dict, pp: Preprocessor) -> dict:
    opts = view["options"]
    bool_opts = [n for n, o in opts.items() if o["type"] == "boolean" and o.get("binding", "compile_time") == "compile_time"]
    results = {}
    for name, o in opts.items():
        if o.get("binding", "compile_time") != "compile_time":
            continue
        aw = o.get("active_when")
        rec = {"option": name, "active_when": aw, "contexts_checked": 0}
        if aw and aw != "false":
            cond = cm.parse_expression(aw)
            vars_ = sorted(cm.referenced_options(cond))
            inert_ok, live_seen, counter = True, False, None
            for bits in itertools.product(*[(0, 1) if opts[v]["type"] == "boolean"
                                            else [opts[v]["default"]] for v in vars_]):
                ctx = dict(zip(vars_, bits))
                full = {n: oo["default"] for n, oo in opts.items()}
                full.update(ctx)
                active = cm.eval_condition(cond, full)
                base = pp.digest(_defines(view, {**ctx, name: o["default"]}))
                diffs = [pp.digest(_defines(view, {**ctx, name: alt})) != base for alt in alternatives(o)]
                rec["contexts_checked"] += 1
                if not active and any(diffs):
                    inert_ok, counter = False, ctx
                if active and any(diffs):
                    live_seen = True
            rec["inert_when_false"] = inert_ok
            rec["live_when_true"] = live_seen
            rec["status"] = "confirmed" if inert_ok and live_seen else "refuted"
            if counter is not None:
                rec["counterexample"] = counter
        else:
            missed = []
            for other in [b for b in bool_opts if b != name]:
                ctx = {other: 1 - opts[other]["default"]}
                base = pp.digest(_defines(view, {**ctx, name: o["default"]}))
                diffs = [pp.digest(_defines(view, {**ctx, name: alt})) != base for alt in alternatives(o)]
                rec["contexts_checked"] += 1
                if not any(diffs):
                    missed.append(ctx)
            base = pp.digest(_defines(view, {name: o["default"]}))
            live_default = any(pp.digest(_defines(view, {name: alt})) != base for alt in alternatives(o))
            rec["live_in_default_context"] = live_default
            rec["inert_in_contexts"] = missed
            rec["status"] = "confirmed" if live_default and not missed else "suspect"
        results[name] = rec
    return results


class Prober:
    """Build library + developer driver with sanitizers and run it."""

    def __init__(self, root: Path, harness: dict, workspace: Path):
        self.root, self.h, self.ws = root, harness, workspace
        self.cache = {}
        import build_and_test as bt
        ok, why = bt.sanitizers_available(harness["build"]["compiler"], SAN[:2])
        self.san = SAN if ok else ["-g", "-O0"]
        self.sanitizers = ok
        if not ok:
            print(f"WARNING: sanitizers unavailable ({why}); probes detect only crashes and build failures",
                  file=sys.stderr)

    def _path(self, p: str) -> str:
        return p.replace("{subject}", str(self.root)).replace("{workspace}", str(self.ws))

    def run(self, view: dict, assign: dict) -> dict:
        key = _defines(view, assign)
        if key in self.cache:
            return self.cache[key]
        b = self.h["build"]
        suite = self.h["test_suite"]
        drv = next(d for d in suite["drivers"] if d["id"] == "unittest")
        full = {n: o["default"] for n, o in view["options"].items()}
        full.update(assign)
        extra = []
        for rule in drv.get("extra_link_sources_when", []):
            cond = cm.as_condition(rule["when"])
            try:
                if cm.eval_condition(cond, full):
                    extra += [self._path(s) for s in rule["sources"]]
            except KeyError:
                pass
        tmp = Path(tempfile.mkdtemp(prefix="probe_"))
        try:
            exe = tmp / ("unittest" + bt_exe_suffix())
            cmd = [b["compiler"], *self.san, f"-I{self.root}", *key,
                   self._path(b["library_sources"][0]), self._path(drv["source"]), *extra, "-o", str(exe)]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                out = {"outcome": "build_failure", "detail": r.stderr.strip().splitlines()[-1][:200]}
            else:
                env = dict(os.environ, ASAN_OPTIONS="detect_leaks=1", UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=0")
                try:
                    rr = subprocess.run([str(exe)], cwd=self._path(suite["working_directory"]),
                                        capture_output=True, text=True, timeout=30, env=env)
                    m = SAN_RE.search(rr.stderr)
                    if m:
                        line = next(l for l in rr.stderr.splitlines() if SAN_RE.search(l))
                        out = {"outcome": "sanitizer", "detail": line.strip()[:200]}
                    elif rr.returncode < 0:
                        out = {"outcome": "crash", "detail": f"signal {-rr.returncode}"}
                    elif rr.returncode != 0:
                        out = {"outcome": "exit_code", "detail": str(rr.returncode)}
                    else:
                        out = {"outcome": "clean"}
                except subprocess.TimeoutExpired:
                    out = {"outcome": "timeout"}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        out["defines"] = list(key)
        self.cache[key] = out
        return out


def bt_exe_suffix() -> str:
    import build_and_test as bt
    return bt.EXE_SUFFIX


def _satisfying(view: dict, cond_text: str | None) -> dict:
    """An assignment of boolean options (others default) under which cond holds."""
    if not cond_text or cond_text == "false":
        return {}
    cond = cm.parse_expression(cond_text)
    vars_ = sorted(cm.referenced_options(cond))
    full = {n: o["default"] for n, o in view["options"].items()}
    for bits in itertools.product(*[(view["options"][v]["default"], 1 - view["options"][v]["default"])
                                    if view["options"][v]["type"] == "boolean" else [view["options"][v]["default"]]
                                    for v in vars_]):
        cand = dict(full, **dict(zip(vars_, bits)))
        if cm.eval_condition(cond, cand):
            return {v: cand[v] for v in vars_}
    return {}


def check_bounds(view: dict, prober: Prober, probe_values=(0, 1, 2, 3)) -> dict:
    out = {}
    for name, o in view["options"].items():
        if o["type"] != "integer" or o.get("binding", "compile_time") != "compile_time":
            continue
        ctx = _satisfying(view, o.get("active_when"))
        rows = []
        for v in sorted(set(probe_values) | {o["default"]}):
            r = prober.run(view, {**ctx, name: v})
            rows.append({"value": v, **{k: r[k] for k in ("outcome", "detail") if k in r}})
        clean_from = None
        for row in reversed(rows):
            if row["outcome"] != "clean":
                break
            clean_from = row["value"]
        out[name] = {"context": ctx, "probes": rows, "min_clean": clean_from,
                     "status": "confirmed" if clean_from is not None and clean_from > min(probe_values) else "inconclusive"}
    return out


def check_constraints(view: dict, prober: Prober) -> dict:
    out = {}
    opts = view["options"]
    for c in view.get("constraints", []):
        if c.get("kind") != "hard":
            continue
        cond = cm.parse_expression(c["expr"])
        vars_ = sorted(cm.referenced_options(cond))
        doms = []
        for v in vars_:
            o = opts[v]
            doms.append(o.get("settings") or ([0, 1] if o["type"] == "boolean" else [o["default"]]))
        violating = None
        for combo in itertools.product(*doms):
            full = {n: oo["default"] for n, oo in opts.items()}
            full.update(zip(vars_, combo))
            if not cm.eval_condition(cond, full):
                violating = dict(zip(vars_, combo))
                break
        if violating is None:
            out[c["id"]] = {"status": "vacuous", "note": "no violating assignment in the discretised domains"}
            continue
        if any(opts[v].get("binding", "compile_time") != "compile_time" for v in violating):
            out[c["id"]] = {"status": "not_probed", "violating": violating,
                            "note": "involves runtime options; needs a driver-level probe"}
            continue
        r = prober.run(view, violating)
        empirical = r["outcome"] != "clean"
        out[c["id"]] = {"violating": violating, "outcome": r["outcome"], "detail": r.get("detail"),
                        "status": "confirmed_empirically" if empirical else "no_failure_observed"}
    return out


def model_view_from_csm(csm) -> dict:
    view = {"options": {}, "constraints": []}
    for n in csm.order:
        o = csm.options[n]
        view["options"][n] = {"type": o["domain_type"], "default": o["default_value"],
                              "settings": csm.settings(n), "binding": o["binding_time"],
                              "active_when": cm.condition_to_text(o["active_when"]).strip("()")
                              if "active_when" in o else None}
    for c in csm.all_constraints:
        view["constraints"].append({"id": c["id"], "kind": c["severity"], "expr": c["expression"]})
    return view


def verify(view: dict, root: Path, harness: dict, workspace: Path, source: str = "ini.c",
           skip_bounds: bool = False) -> dict:
    pp = Preprocessor(root, source, ["."])
    res = {"stage": "S5_verify", "inertness": check_inertness(view, pp)}
    prober = Prober(root, harness, workspace)
    res["bounds"] = {} if skip_bounds else check_bounds(view, prober)
    res["constraints"] = check_constraints(view, prober)
    res["preprocessor_runs"] = len(pp.cache)
    res["probe_builds"] = len(prober.cache)
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--view", help="model view JSON (from merge.py --view)")
    ap.add_argument("--csm", help="verify an existing CSM instead")
    ap.add_argument("--harness", required=True, help="csm.json/yaml providing build + test_suite")
    ap.add_argument("--source", default="ini.c")
    ap.add_argument("--skip-bounds", action="store_true")
    ap.add_argument("-o", "--output")
    a = ap.parse_args(argv)
    harness_csm = cm.load_csm(a.harness)
    harness = {"build": harness_csm.data["build"], "test_suite": harness_csm.raw["test_suite"]}
    view = json.loads(Path(a.view).read_text()) if a.view else model_view_from_csm(cm.load_csm(a.csm))
    res = verify(view, Path(a.root).resolve(), harness, harness_csm.path.parent.resolve(), a.source, a.skip_bounds)
    text = json.dumps(res, indent=2)
    if a.output:
        Path(a.output).write_text(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
