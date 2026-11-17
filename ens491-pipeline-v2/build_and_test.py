#!/usr/bin/env python3
"""Build, test and measure coverage of the subject system across configurations.

Reads csm.json (via validate_constraints.py) and, for each requested
configuration:

  1. validates it against the CSM constraints (invalid ones are recorded and
     skipped unless --force-invalid);
  2. groups configurations by their *effective* compile-time settings (inert
     options collapsed to defaults), so the library is compiled once per group
     with gcc --coverage -fprofile-arcs -ftest-coverage and the -D macros;
  3. per configuration: resets the coverage counters, runs every compatible
     developer test driver (golden-baseline oracle where a developer variant
     matches), snapshots coverage, then runs the generated runtime driver with
     the configuration's runtime options (baseline + differential oracles) and
     snapshots coverage again;
  4. writes results/summary.json (latest run) plus per-run artefacts under
     results/runs/<run_id>/ (build logs, test stdout/stderr, gcov JSON,
     lcov tracefiles).

Selecting configurations (one of):
  --default                     the CSM default configuration
  --config JSON                 one (partial) configuration
  --configs FILE                JSON list of (partial) configurations
  --sample random:N             N valid configurations, uniform over settings
  --sample each-choice          every setting of every option at least once
  --developer-variants          the compile-time settings of every developer
                                test variant (reproduces tests/unittest.sh)

Other flags: --seed, --no-sanitize, --strict, --force-invalid, --timeout,
             --csm, --results-dir, --quiet.
"""

from __future__ import annotations

import argparse
import difflib
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import csm_model as cm  # noqa: E402
import validate_constraints as vc  # noqa: E402

SANITIZER_RE = re.compile(
    r"ERROR: AddressSanitizer|ERROR: LeakSanitizer|runtime error:|"
    r"SUMMARY: UndefinedBehaviorSanitizer|Assertion `.*' failed")
DIAG_RE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+): (?P<kind>warning|error): "
    r"(?P<msg>.*?)(?: \[(?P<flag>-W[^\]]+)\])?$", re.M)
SAN_EXITCODE = 86
ENV_SAN = {
    "ASAN_OPTIONS": f"detect_leaks=1:abort_on_error=0:exitcode={SAN_EXITCODE}",
    "UBSAN_OPTIONS": f"halt_on_error=1:print_stacktrace=1:exitcode={SAN_EXITCODE}",
}


# --------------------------------------------------------------------------
# Paths and small helpers
# --------------------------------------------------------------------------

class Ctx:
    def __init__(self, csm: vc.CSM, args):
        self.csm = csm
        self.args = args
        self.workspace = csm.path.parent.resolve()
        self.subject = (self.workspace / csm.data["system"]["local_path"]).resolve()
        self.build = csm.data["build"]
        self.suite = csm.data.get("test_suite", {"drivers": [], "variants": []})
        self.results_dir = Path(args.results_dir).resolve()
        self.run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
        self.run_dir = self.results_dir / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def path(self, p: str) -> Path:
        return Path(p.replace("{subject}", str(self.subject)).replace("{workspace}", str(self.workspace)))

    def rel(self, p: Path) -> str:
        p = Path(p)
        for base, tag in ((self.subject, "{subject}"), (self.workspace, "{workspace}")):
            try:
                return f"{tag}/{p.resolve().relative_to(base)}"
            except ValueError:
                pass
        return str(p)


def c_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


EXE_SUFFIX = ".exe" if (os.name == "nt" or sys.platform.startswith(("win", "cygwin", "msys"))) else ""


def sanitizers_available(compiler: str, flags: list[str]) -> tuple[bool, str]:
    """Compile and link a trivial program with the sanitizer flags. MinGW (Windows) GCC, for
    example, accepts -fsanitize=address but ships no libasan, so linking fails."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "probe.c"
        src.write_text("int main(void) { return 0; }\n")
        try:
            r = subprocess.run([compiler, *flags, str(src), "-o", str(Path(td) / ("probe" + EXE_SUFFIX))],
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        if r.returncode != 0:
            return False, (r.stderr.strip().splitlines() or ["link failed"])[-1][:200]
    return True, ""


def macro_flags(csm: vc.CSM, cfg: dict) -> list[str]:
    flags = []
    for name in csm.compile_time_options():
        enc = csm.options[name]["encoding"]
        v = cfg[name]
        fmt = enc["value_format"]
        if fmt == "c_string_literal":
            rendered = c_string(v)
        elif fmt in ("int", "bool01"):
            rendered = str(int(v))
        else:
            rendered = str(v)
        flags.append(f"-D{enc['macro']}={rendered}")
    return flags


def run(cmd: list[str], cwd: Path, timeout: float, env: dict | None = None) -> dict:
    t0 = time.monotonic()
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout, env=full_env)
        out, err, rc, to = p.stdout, p.stderr, p.returncode, False
    except subprocess.TimeoutExpired as e:
        out, err, rc, to = e.stdout or b"", e.stderr or b"", None, True
    return {
        "cmd": cmd, "returncode": rc, "timeout": to,
        "stdout": out.decode("utf-8", "replace"), "stderr": err.decode("utf-8", "replace"),
        "seconds": round(time.monotonic() - t0, 3),
    }


def parse_diagnostics(stderr: str, ctx: Ctx) -> tuple[list, list]:
    warnings, errors = [], []
    for m in DIAG_RE.finditer(stderr):
        d = {"file": ctx.rel(Path(m["file"])) if os.path.isabs(m["file"]) else m["file"],
             "line": int(m["line"]), "column": int(m["col"]), "message": m["msg"], "flag": m["flag"]}
        (warnings if m["kind"] == "warning" else errors).append(d)
    if not errors and "error" in stderr.lower() and "collect2" in stderr:
        errors.append({"file": None, "line": None, "column": None,
                       "message": stderr.strip().splitlines()[-1], "flag": None})
    return warnings, errors


def classify_process(res: dict, ok_codes=(0,)) -> dict | None:
    """Return a crash description, or None if the process ended normally."""
    if res["timeout"]:
        return {"kind": "timeout"}
    rc = res["returncode"]
    san = SANITIZER_RE.search(res["stderr"])
    if san:
        kind = "assertion" if san.group(0).startswith("Assertion") else "sanitizer"
        lines = [l for l in res["stderr"].splitlines() if SANITIZER_RE.search(l) or "SUMMARY" in l]
        return {"kind": kind, "returncode": rc, "report": lines[:6]}
    if rc is not None and rc < 0:
        return {"kind": "signal", "signal": -rc}
    if rc not in ok_codes:
        return {"kind": "exit_code", "returncode": rc}
    return None


def split_cases(text: str, delim: re.Pattern) -> "OrderedDict[str, list[str]]":
    cases: "OrderedDict[str, list[str]]" = OrderedDict()
    block: list[str] = []
    for line in text.splitlines():
        block.append(line)
        m = delim.match(line)
        if m:
            name = m.group(1)
            while name in cases:  # keep duplicates distinct
                name += "'"
            cases[name] = block
            block = []
    if block:
        cases["<trailing-output>"] = block
    return cases


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------

def group_key(csm: vc.CSM, eff: dict) -> tuple[str, dict]:
    ct = {n: eff[n] for n in csm.compile_time_options()}
    return hashlib.sha1(json.dumps(ct, sort_keys=True).encode()).hexdigest()[:10], ct


def build_group(ctx: Ctx, eff: dict, bdir: Path, sanitize: bool, coverage: bool) -> dict:
    """Compile the library + drivers for one compile-time group.

    coverage=True  -> library compiled with coverage_cflags (gcov counters)
    sanitize=True  -> library and drivers compiled with sanitizer_cflags
    """
    csm, b = ctx.csm, ctx.build
    bdir.mkdir(parents=True, exist_ok=True)
    flags = macro_flags(csm, eff)
    san = b.get("sanitizer_cflags", []) if sanitize else []
    cov = b.get("coverage_cflags", []) if coverage else []
    incs = [f"-I{ctx.path(i)}" for i in b.get("include_dirs", [])]
    base = [b["compiler"]] + b.get("cflags", []) + san
    rec = {"defines": flags, "library": [], "drivers": {}, "status": "success",
           "warnings": [], "errors": [], "sanitizers": sanitize, "coverage": coverage}

    objects = []
    for src in b["library_sources"]:
        s = ctx.path(src)
        obj = bdir / (s.stem + ".o")
        cmd = base + cov + incs + flags + ["-c", str(s), "-o", str(obj)]
        r = run(cmd, bdir, 300)
        w, e = parse_diagnostics(r["stderr"], ctx)
        ok = r["returncode"] == 0
        rec["library"].append({"source": ctx.rel(s), "status": "success" if ok else "failure",
                               "command": " ".join(cmd), "warnings": w, "errors": e,
                               "seconds": r["seconds"]})
        rec["warnings"] += w
        rec["errors"] += e
        if not ok:
            rec["status"] = "failure"
            (bdir / "build.log").write_text(r["stderr"])
        objects.append(str(obj))
    if rec["status"] != "success":
        return rec

    for drv in ctx.suite.get("drivers", []):
        d = {"status": None, "warnings": [], "errors": []}
        rec["drivers"][drv["id"]] = d
        if "compatible_when" in drv and not vc.eval_condition(drv["compatible_when"], eff):
            d["status"] = "skipped_incompatible"
            d["reason"] = vc.condition_to_text(drv["compatible_when"]) + " is false"
            continue
        extra = []
        for rule in drv.get("extra_link_sources_when", []):
            if vc.eval_condition(rule["when"], eff):
                extra += [str(ctx.path(s)) for s in rule["sources"]]
        exe = bdir / (drv["id"] + EXE_SUFFIX)
        cmd = base + incs + flags + [str(ctx.path(drv["source"]))] + extra + objects + \
            (["--coverage"] if coverage else []) + ["-o", str(exe)]
        r = run(cmd, bdir, 300)
        w, e = parse_diagnostics(r["stderr"], ctx)
        d.update(status="success" if r["returncode"] == 0 else "failure",
                 command=" ".join(cmd), warnings=w, errors=e, executable=str(exe))
        if r["returncode"] != 0:
            (bdir / f"{drv['id']}.build.log").write_text(r["stderr"])
    return rec


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------

def _gcov_json(ctx: Ctx, bdir: Path) -> list | None:
    out = []
    for s_ in [ctx.path(x) for x in ctx.build["library_sources"]]:
        r = run(["gcov", "-b", "--json-format", "--stdout", "-o", str(bdir), str(s_)], bdir, 120)
        if r["returncode"] != 0 or not r["stdout"].strip():
            return None
        out.append(json.loads(r["stdout"]))
    return out


def _cov_files(ctx: Ctx, files_json: list, bdir: Path):
    cov_names = set(ctx.build.get("coverage_sources", [Path(x).name for x in ctx.build["library_sources"]]))
    for data in files_json:
        for f in data["files"]:
            fpath = Path(f["file"])
            if not fpath.is_absolute():
                fpath = Path(data.get("current_working_directory", bdir)) / fpath
            if fpath.name in cov_names:
                yield ctx.rel(fpath), f


def gcov_snapshot(ctx: Ctx, bdir: Path, out_prefix: Path, write_files: bool = True) -> dict | None:
    """Line, branch and function coverage of the SUT sources from the current .gcda counters.

    Statement coverage = executed lines / executable lines (gcov 'lines');
    branch coverage    = branch outcomes taken at least once / all branch outcomes;
    function coverage  = functions entered / functions."""
    files_json = _gcov_json(ctx, bdir)
    if files_json is None:
        return None
    lines_total = lines_hit = br_total = br_hit = fn_total = fn_hit = 0
    per_line, branches, functions = {}, {}, []
    for key, f in _cov_files(ctx, files_json, bdir):
        for ln in f["lines"]:
            lines_total += 1
            lines_hit += ln["count"] > 0
            taken = [b["count"] > 0 for b in ln["branches"]]
            br_total += len(taken)
            br_hit += sum(taken)
            per_line[f"{key}:{ln['line_number']}"] = {"count": ln["count"], "branches": len(taken),
                                                      "taken": sum(taken)}
            for i, t in enumerate(taken):
                branches[f"{key}:{ln['line_number']}:{i}"] = t
        for fn in f.get("functions", []):
            fl = [ln for ln in f["lines"] if fn["start_line"] <= ln["line_number"] <= fn["end_line"]]
            fb = [b["count"] > 0 for ln in fl for b in ln["branches"]]
            fn_total += 1
            fn_hit += fn["execution_count"] > 0
            functions.append({"file": key, "function": fn.get("demangled_name", fn["name"]),
                              "start_line": fn["start_line"], "end_line": fn["end_line"],
                              "calls": fn["execution_count"],
                              "lines_covered": sum(1 for ln in fl if ln["count"] > 0), "lines_total": len(fl),
                              "branches_covered": sum(fb), "branches_total": len(fb),
                              "blocks_executed": fn.get("blocks_executed"), "blocks": fn.get("blocks")})
    pct = lambda a, b: round(100.0 * a / b, 2) if b else None  # noqa: E731
    res = {
        "statement": {"covered": lines_hit, "total": lines_total, "percent": pct(lines_hit, lines_total)},
        "branch": {"covered": br_hit, "total": br_total, "percent": pct(br_hit, br_total)},
        "function": {"covered": fn_hit, "total": fn_total, "percent": pct(fn_hit, fn_total)},
        "functions": functions,
        "_per_line": per_line,
        "_branches": branches,
    }
    if write_files:
        out_prefix.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(str(out_prefix) + ".gcov.json.gz", "wt") as fh:
            json.dump(files_json, fh)
        cov_names = set(ctx.build.get("coverage_sources", [Path(x).name for x in ctx.build["library_sources"]]))
        write_lcov(files_json, cov_names, ctx, Path(str(out_prefix) + ".info"))
        write_coverage_table(res, Path(str(out_prefix) + ".table.md"), Path(str(out_prefix) + ".table.csv"))
        res["gcov_json"] = ctx.rel(Path(str(out_prefix) + ".gcov.json.gz"))
        res["lcov_tracefile"] = ctx.rel(Path(str(out_prefix) + ".info"))
        res["table"] = ctx.rel(Path(str(out_prefix) + ".table.md"))
    return res


def write_coverage_table(res: dict, md: Path, csv: Path) -> None:
    pc = lambda a, b: f"{100.0 * a / b:.1f}%" if b else "-"  # noqa: E731
    rows = ["| Function | Lines | Calls | Line cov. | Branch cov. |", "|---|---|---|---|---|"]
    out = ["file,function,start_line,end_line,calls,lines_covered,lines_total,branches_covered,branches_total"]
    for f in res["functions"]:
        rows.append(f"| {f['function']} | {f['start_line']}-{f['end_line']} | {f['calls']} | "
                    f"{f['lines_covered']}/{f['lines_total']} ({pc(f['lines_covered'], f['lines_total'])}) | "
                    f"{f['branches_covered']}/{f['branches_total']} ({pc(f['branches_covered'], f['branches_total'])}) |")
        out.append(",".join(str(f[k]) for k in ("file", "function", "start_line", "end_line", "calls",
                                                  "lines_covered", "lines_total", "branches_covered",
                                                  "branches_total")))
    s, b, fn = res["statement"], res["branch"], res["function"]
    rows.append(f"| **total** | | | {s['covered']}/{s['total']} ({s['percent']}%) | "
                f"{b['covered']}/{b['total']} ({b['percent']}%) |")
    rows.append("")
    rows.append(f"Function coverage: {fn['covered']}/{fn['total']} ({fn['percent']}%)")
    md.write_text("\n".join(rows) + "\n")
    csv.write_text("\n".join(out) + "\n")


def compact_coverage(res: dict) -> dict:
    """Executed lines / taken branches in compact form (for per-case and index files)."""
    lines = {}
    for k, v in res["_per_line"].items():
        f, ln = k.rsplit(":", 1)
        if v["count"] > 0:
            lines.setdefault(f, []).append(int(ln))
    br = sorted(k for k, t in res["_branches"].items() if t)
    compiled = {}
    for k in res["_per_line"]:
        f, ln = k.rsplit(":", 1)
        compiled.setdefault(f, []).append(int(ln))
    return {"lines": {f: cm.format_lines(ls) for f, ls in lines.items()}, "branches_taken": br,
            "lines_compiled": {f: cm.format_lines(ls) for f, ls in compiled.items()},
            "statement_percent": res["statement"]["percent"], "branch_percent": res["branch"]["percent"]}


def write_lcov(files_json: list, cov_names: set, ctx: Ctx, path: Path) -> None:
    out: list[str] = []
    for data in files_json:
        for f in data["files"]:
            if Path(f["file"]).name not in cov_names:
                continue
            out += ["TN:", f"SF:{f['file']}"]
            for fn in f.get("functions", []):
                out.append(f"FN:{fn['start_line']},{fn['name']}")
            for fn in f.get("functions", []):
                out.append(f"FNDA:{fn['execution_count']},{fn['name']}")
            out.append(f"FNF:{len(f.get('functions', []))}")
            out.append(f"FNH:{sum(1 for fn in f.get('functions', []) if fn['execution_count'] > 0)}")
            brf = brh = 0
            for ln in f["lines"]:
                for i, br in enumerate(ln["branches"]):
                    taken = str(br["count"]) if ln["count"] > 0 else "-"
                    out.append(f"BRDA:{ln['line_number']},0,{i},{taken}")
                    brf += 1
                    brh += br["count"] > 0
            out.append(f"BRF:{brf}")
            out.append(f"BRH:{brh}")
            for ln in f["lines"]:
                out.append(f"DA:{ln['line_number']},{ln['count']}")
            out.append(f"LF:{len(f['lines'])}")
            out.append(f"LH:{sum(1 for ln in f['lines'] if ln['count'] > 0)}")
            out.append("end_of_record")
    path.write_text("\n".join(out) + "\n")


def reset_counters(bdir: Path) -> None:
    for g in bdir.glob("*.gcda"):
        g.unlink()


# --------------------------------------------------------------------------
# Test execution
# --------------------------------------------------------------------------

def matching_variants(ctx: Ctx, eff: dict) -> dict:
    """driver id -> developer variant whose effective compile-time settings equal eff's."""
    csm = ctx.csm
    ct = csm.compile_time_options()
    out = {}
    for v in ctx.suite.get("variants", []):
        full, _ = vc.complete(csm, v["settings"])
        veff = vc.effective_config(csm, full)
        if all(veff[n] == eff[n] for n in ct):
            out.setdefault(v["driver"], v)
    return out


def prepare_sandbox(ctx: Ctx, sandbox: Path, extra_inputs: list[Path]) -> None:
    sandbox.mkdir(parents=True, exist_ok=True)
    wd = ctx.path(ctx.suite.get("working_directory", "{subject}/tests"))
    for f in wd.glob("*.ini"):
        shutil.copy2(f, sandbox / f.name)
    for f in extra_inputs:
        shutil.copy2(f, sandbox / f.name)


def judge_cases(actual: str, expected: str | None, delim: re.Pattern, crash: dict | None,
                driver: str, oracle_name: str, restrict_to: set | None = None) -> list[dict]:
    act = split_cases(actual, delim)
    exp = split_cases(expected, delim) if expected is not None else None
    cases = []
    names = list(exp.keys()) if exp is not None else list(act.keys())
    for name in list(act.keys()):
        if name not in names:
            names.append(name)
    for name in names:
        c = {"driver": driver, "case": name, "oracles": {}}
        a = act.get(name)
        if exp is not None and (restrict_to is None or name in restrict_to) and name in exp:
            if a is None:
                c["oracles"][oracle_name] = "missing_output"
            elif a == exp[name]:
                c["oracles"][oracle_name] = "pass"
            else:
                c["oracles"][oracle_name] = "fail"
                c["diff"] = list(difflib.unified_diff(exp[name], a, "expected", "actual", lineterm="", n=1))[:30]
        elif exp is not None and name not in exp and a is not None and name != "<trailing-output>" \
                and restrict_to is None:
            c["oracles"][oracle_name] = "unexpected_case"
        if a is None:
            c["status"] = "crash" if crash else "not_run"
        elif name == "<trailing-output>":
            c["status"] = "crash" if crash else "no_oracle"
        cases.append(c)
    return cases


def finalize_status(c: dict) -> None:
    if "status" in c:
        return
    verdicts = set(c["oracles"].values())
    if verdicts & {"fail", "unexpected_case", "missing_output"}:
        c["status"] = "fail"
    elif "pass" in verdicts:
        c["status"] = "pass"
    else:
        c["status"] = "no_oracle"


def driver_argv(ctx: Ctx, drv: dict, cfg: dict, extra_inputs: list[str] | None = None,
                only: list[str] | None = None) -> list[str]:
    """Command line for a driver. `extra_inputs` (per-configuration test inputs, e.g. LLM-generated)
    are appended for drivers that accept runtime options; `only` restricts the inputs to a subset."""
    argv: list[str] = []
    if drv.get("accepts_runtime_options"):
        for name in ctx.csm.runtime_options():
            enc = ctx.csm.options[name]["encoding"]
            if enc["mechanism"] == "driver_cli":
                argv += [enc["flag"], str(cfg[name])]
        argv.append("--")
    inputs = []
    for a in drv.get("args", []):
        if a == "@input_files":
            inputs += ctx.suite.get("input_files", [])
        else:
            inputs.append(ctx.path(a).name)
    if drv.get("accepts_runtime_options"):
        inputs += [Path(x).name for x in (extra_inputs or [])]
    if only is not None:
        inputs = [i for i in inputs if i in only]
    return argv + inputs


def ordered_drivers(ctx: Ctx) -> list[dict]:
    """Developer drivers first, then generated ones (coverage is snapshotted in between)."""
    ds = ctx.suite.get("drivers", [])
    return [d for d in ds if d["origin"] == "developer"] + [d for d in ds if d["origin"] == "generated"]


def run_tests(ctx: Ctx, cfg: dict, eff: dict, verdict_build: dict, cov_build: dict,
              cfg_dir: Path, meta: dict | None = None) -> tuple[dict, dict]:
    """Verdict pass on verdict_build (sanitizers when enabled); coverage pass on
    cov_build. When both are the same build, one pass serves both purposes."""
    suite = ctx.suite
    delim = re.compile(suite.get("case_delimiter_regex", r"^(.+?): e=-?\d+.*$"))
    variants = matching_variants(ctx, eff)
    timeout = ctx.args.timeout
    sandbox = cfg_dir / "sandbox"
    meta = meta or {}
    extra_inputs = [str(ctx.path(x)) for x in meta.get("extra_inputs", [])]
    expectations = meta.get("expectations", {})
    test_env = {k: str(v) for k, v in (meta.get("env") or {}).items()}
    gen_inputs = [ctx.path(a) for d in suite.get("drivers", []) for a in d.get("args", [])
                  if a.startswith("{workspace}") or a.startswith("{subject}")]
    prepare_sandbox(ctx, sandbox, gen_inputs + [Path(x) for x in extra_inputs])
    same = verdict_build is cov_build

    cases, processes, coverage = [], [], {}

    def run_driver(build: dict, drv: dict, argv: list[str], ok_codes, tag: str):
        exe = build["drivers"][drv["id"]]["executable"]
        env = dict(ENV_SAN) if build["sanitizers"] else {}
        env.update(test_env)
        r = run([exe] + argv, sandbox, timeout, env or None)
        (cfg_dir / f"{drv['id']}{tag}.stdout.txt").write_text(r["stdout"])
        (cfg_dir / f"{drv['id']}{tag}.stderr.txt").write_text(r["stderr"])
        return r, classify_process(r, ok_codes)

    def unavailable(build: dict, drv: dict) -> dict | None:
        binfo = build["drivers"].get(drv["id"], {})
        if binfo.get("status") == "success":
            return None
        return {"driver": drv["id"], "case": "<driver>", "oracles": {},
                "status": "skipped" if binfo.get("status") == "skipped_incompatible" else "build_failed",
                "reason": binfo.get("reason") or "driver failed to build"}

    # ---------------- verdict pass ----------------
    if same:
        reset_counters(Path(cov_build["dir"]))
    dev_done = False
    for drv in ordered_drivers(ctx):
        if same and drv["origin"] == "generated" and not dev_done:
            dev_done = True
            snap = gcov_snapshot(ctx, Path(cov_build["dir"]), cfg_dir / "coverage_developer")
            if snap:
                coverage["developer_suite"] = snap
        skip = unavailable(verdict_build, drv)
        if skip:
            cases.append(skip)
            continue
        argv = driver_argv(ctx, drv, cfg, extra_inputs)
        ok_codes = (0, 1) if drv["origin"] == "generated" else (0,)
        r, crash = run_driver(verdict_build, drv, argv, ok_codes, "")
        processes.append({"driver": drv["id"], "argv": argv, "returncode": r["returncode"],
                          "seconds": r["seconds"], "sanitizers": verdict_build["sanitizers"], "crash": crash})

        expected, oname, restrict = None, "baseline", None
        if drv["origin"] == "developer":
            var = variants.get(drv["id"])
        else:
            proxy = drv.get("baseline_proxy_for")
            ok = proxy and vc.eval_condition(drv.get("baseline_valid_when", True), cfg)
            var = variants.get(proxy) if ok else None
        if var:
            expected = ctx.path(var["baseline"]).read_text()
            oname = f"baseline:{var['id']}"
            if drv["origin"] == "generated":
                restrict = set(split_cases(expected, delim).keys())
        cs = judge_cases(r["stdout"], expected, delim, crash, drv["id"], oname, restrict)

        diff = {}
        for line in r["stderr"].splitlines():
            m = re.match(r"^DIFF_ORACLE (\S+) (PASS|FAIL|SKIP)(.*)$", line)
            if m:
                diff[m.group(1)] = (m.group(2).lower(), m.group(3).strip())
        for c in cs:
            if c["case"] in diff:
                verdict, detail = diff[c["case"]]
                if verdict != "skip":
                    c["oracles"]["differential"] = verdict
                if verdict == "fail":
                    c["differential_detail"] = detail
        if drv["origin"] == "generated" and expectations:
            act = split_cases(r["stdout"], delim)
            for c in cs:
                exp = expectations.get(c["case"])
                if exp is None or c["case"] not in act or exp.get("driver", drv["id"]) != drv["id"]:
                    continue
                if isinstance(exp, dict) and "contains" in exp:
                    got = "\n".join(act[c["case"]])
                    missing = [x for x in exp["contains"] if x not in got]
                    c["oracles"]["expected"] = "fail" if missing else "pass"
                    if missing:
                        c["expected_missing"] = missing
                elif isinstance(exp, dict) and "lines" in exp:
                    want = list(exp["lines"])
                    c["oracles"]["expected"] = "pass" if act[c["case"]] == want else "fail"
                    if act[c["case"]] != want:
                        c["diff"] = list(difflib.unified_diff(want, act[c["case"]], "expected", "actual",
                                                              lineterm="", n=1))[:30]
        if crash:
            cs.append({"driver": drv["id"], "case": "<process>", "oracles": {}, "status": "crash",
                       "crash": crash})
        cases += cs
    if same:
        if not dev_done:
            snap = gcov_snapshot(ctx, Path(cov_build["dir"]), cfg_dir / "coverage_developer")
            if snap:
                coverage["developer_suite"] = snap
        snap = gcov_snapshot(ctx, Path(cov_build["dir"]), cfg_dir / "coverage_all")
        if snap:
            coverage["all_tests"] = snap

    # ---------------- coverage pass (separate, sanitizer-free build) ----------------
    if not same and cov_build["status"] == "success":
        bdir = Path(cov_build["dir"])
        reset_counters(bdir)
        dev_done = False
        for drv in ordered_drivers(ctx):
            if drv["origin"] == "generated" and not dev_done:
                dev_done = True
                snap = gcov_snapshot(ctx, bdir, cfg_dir / "coverage_developer")
                if snap:
                    coverage["developer_suite"] = snap
            if unavailable(cov_build, drv):
                continue
            argv = driver_argv(ctx, drv, cfg, extra_inputs)
            ok_codes = (0, 1) if drv["origin"] == "generated" else (0,)
            r, crash = run_driver(cov_build, drv, argv, ok_codes, ".coverage_run")
            processes.append({"driver": drv["id"], "argv": argv, "returncode": r["returncode"],
                              "seconds": r["seconds"], "sanitizers": False, "purpose": "coverage",
                              "crash": crash})
        if not dev_done:
            snap = gcov_snapshot(ctx, bdir, cfg_dir / "coverage_developer")
            if snap:
                coverage["developer_suite"] = snap
        snap = gcov_snapshot(ctx, bdir, cfg_dir / "coverage_all")
        if snap:
            coverage["all_tests"] = snap

    # ---------------- per-test-case coverage (generated driver, one input at a time) ----------------
    if getattr(ctx.args, "per_case", True) and cov_build["status"] == "success":
        bdir = Path(cov_build["dir"])
        per_case = {}
        for drv in ordered_drivers(ctx):
            if drv["origin"] != "generated" or unavailable(cov_build, drv) or not drv.get("accepts_runtime_options"):
                continue
            all_inputs = driver_argv(ctx, drv, cfg, extra_inputs)
            inputs = all_inputs[all_inputs.index("--") + 1:] if "--" in all_inputs else all_inputs
            for inp in inputs:
                reset_counters(bdir)
                argv = driver_argv(ctx, drv, cfg, extra_inputs, only=[inp])
                exe = cov_build["drivers"][drv["id"]]["executable"]
                run([exe] + argv, sandbox, timeout, test_env or None)
                snap = gcov_snapshot(ctx, bdir, cfg_dir / "unused", write_files=False)
                if snap:
                    per_case[f"{drv['id']}/{inp}"] = compact_coverage(snap)
        if per_case:
            (cfg_dir / "per_case_coverage.json").write_text(json.dumps(per_case, indent=1))
            coverage["per_case_file"] = ctx.rel(cfg_dir / "per_case_coverage.json")

    for c in cases:
        finalize_status(c)
    counts = Counter(c["status"] for c in cases)
    tests = {
        "total": len(cases),
        "passed": counts.get("pass", 0),
        "failed": counts.get("fail", 0),
        "crashed": counts.get("crash", 0),
        "no_oracle": counts.get("no_oracle", 0),
        "not_run": counts.get("not_run", 0),
        "skipped": counts.get("skipped", 0),
        "build_failed": counts.get("build_failed", 0),
        "baseline_variants_used": sorted({k.split(":", 1)[1] for c in cases for k in c["oracles"]
                                          if k.startswith("baseline:")}),
        "crashes": [p for p in processes if p["crash"] and p.get("purpose") != "coverage"],
        "processes": processes,
        "cases": cases,
    }
    return tests, coverage


# --------------------------------------------------------------------------
# Configuration selection
# --------------------------------------------------------------------------

def collect_configs(csm: vc.CSM, args) -> list[dict]:
    if args.default:
        return [{}]
    if args.config:
        return [json.loads(args.config)]
    if args.configs:
        data = cm.read_model_file(args.configs)
        if isinstance(data, dict) and isinstance(data.get("configs"), list):
            data = data["configs"]
        return data if isinstance(data, list) else [data]
    if args.developer_variants:
        suite = csm.data.get("test_suite", {})
        seen, out = set(), []
        for v in suite.get("variants", []):
            key = json.dumps(v["settings"], sort_keys=True)
            if key not in seen:
                seen.add(key)
                out.append(dict(v["settings"]))
        return out
    if args.sample:
        kind, _, n = args.sample.partition(":")
        if kind == "random":
            return vc.sample_random(csm, int(n or 10), seed=args.seed)
        if kind == "each-choice":
            return vc.sample_each_choice(csm, seed=args.seed)
        raise SystemExit(f"unknown --sample kind: {args.sample}")
    raise SystemExit("no configuration selected")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def tool_versions() -> dict:
    def first_line(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()[0]
        except Exception:  # noqa: BLE001
            return None
    return {"gcc": first_line(["gcc", "--version"]), "gcov": first_line(["gcov", "--version"]),
            "python": sys.version.split()[0]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument("--default", action="store_true")
    sel.add_argument("--config")
    sel.add_argument("--configs")
    sel.add_argument("--sample")
    sel.add_argument("--developer-variants", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--csm", default=str(HERE / "csm.json"))
    ap.add_argument("--results-dir", default=str(HERE / "results"))
    ap.add_argument("--no-sanitize", action="store_true", help="build without ASan/UBSan")
    ap.add_argument("--strict", action="store_true", help="soft-constraint violations make a config invalid")
    ap.add_argument("--force-invalid", action="store_true", help="build and test invalid configurations too")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--keep-builds", action="store_true", help="keep object files, executables and sandboxes")
    ap.add_argument("--no-per-case", dest="per_case", action="store_false",
                    help="skip per-test-case coverage of the generated driver")
    ap.add_argument("--label", default=None, help="free-text label stored in the summary")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    csm = vc.load_csm(args.csm)
    sanitizer_note = None
    if not args.no_sanitize:
        ok_san, why = sanitizers_available(csm.data["build"]["compiler"], csm.data["build"].get("sanitizer_cflags", []))
        if not ok_san:
            args.no_sanitize = True
            sanitizer_note = (f"sanitizers unavailable with this compiler ({why}); running without ASan/UBSan, so "
                              f"memory errors are NOT detected. Use Linux or WSL for full results.")
            print("WARNING: " + sanitizer_note, file=sys.stderr)
    ctx = Ctx(csm, args)
    requested = collect_configs(csm, args)
    started = datetime.now(timezone.utc)

    # 1. validate
    entries = []
    for i, raw in enumerate(requested, 1):
        meta = {}
        if isinstance(raw, dict) and "configuration" in raw:
            meta = {k: v for k, v in raw.items() if k != "configuration"}
            raw = raw["configuration"]
        rep = vc.validate(csm, raw, strict=args.strict)
        entries.append({"config_id": meta.get("config_id") or f"cfg-{i:03d}", "requested": raw,
                        "report": rep, "meta": meta})

    # 2. group by effective compile-time settings
    groups: "OrderedDict[str, dict]" = OrderedDict()
    for e in entries:
        rep = e["report"]
        if not rep["valid"] and not args.force_invalid:
            continue
        eff = rep["effective_configuration"]
        key, ct = group_key(csm, eff)
        groups.setdefault(key, {"compile_time": ct, "members": []})["members"].append(e)

    results = []
    cumulative: dict = {}
    index = {"configs": {}, "configs_developer": {}, "executable_lines": set(), "branches": set()}
    for key, g in groups.items():
        bdir = ctx.run_dir / "builds" / key
        eff0 = g["members"][0]["report"]["effective_configuration"]
        if not args.quiet:
            print(f"[build {key}] {len(g['members'])} configuration(s); "
                  f"non-default: {non_default(csm, g['compile_time']) or 'none'}", flush=True)
        cov_build = build_group(ctx, eff0, bdir / "coverage", sanitize=False, coverage=True)
        cov_build["dir"] = str(bdir / "coverage")
        if args.no_sanitize:
            ver_build = cov_build
        else:
            ver_build = build_group(ctx, eff0, bdir / "sanitizer", sanitize=True, coverage=False)
            ver_build["dir"] = str(bdir / "sanitizer")
        ok = cov_build["status"] == "success" and ver_build["status"] == "success"

        def strip(b: dict) -> dict:
            return {"status": b["status"], "warnings": b["warnings"], "errors": b["errors"],
                    "library": b["library"],
                    "drivers": {k: {kk: vv for kk, vv in v.items() if kk != "executable"}
                                for k, v in b["drivers"].items()}}
        for idx, e in enumerate(g["members"]):
            rep = e["report"]
            cfg, eff = rep["configuration"], rep["effective_configuration"]
            cfg_dir = ctx.run_dir / e["config_id"]
            cfg_dir.mkdir(parents=True, exist_ok=True)
            build_rec = {"status": "success" if ok else "failure", "rebuilt": idx == 0,
                         "defines": cov_build["defines"], **strip(cov_build)}
            build_rec["status"] = "success" if ok else "failure"
            if not args.no_sanitize:
                build_rec["sanitizer_build"] = strip(ver_build)
            res = {
                "config_id": e["config_id"],
                "provenance": {k: v for k, v in e["meta"].items()
                               if k not in ("config_id", "extra_inputs", "expectations", "env")},
                "extra_inputs": e["meta"].get("extra_inputs", []),
                "configuration": cfg,
                "non_default_settings": non_default(csm, cfg),
                "validity": {k: rep[k] for k in ("valid", "errors", "warnings", "violated_constraints", "inert_options")},
                "compile_group": key,
                "effective_compile_time": g["compile_time"],
                "runtime": {n: cfg[n] for n in csm.runtime_options()},
                "build": build_rec,
            }
            build = {"status": "success" if ok else "failure"}
            if build["status"] == "success":
                tests, cov = run_tests(ctx, cfg, eff, ver_build, cov_build, cfg_dir, e["meta"])
                res["tests"] = tests
                for k, c in cov.items():
                    if k == "all_tests":
                        for ln, info in c["_per_line"].items():
                            cur = cumulative.setdefault(ln, 0)
                            cumulative[ln] = cur + info["count"]
                        index["configs"][e["config_id"]] = compact_coverage(c)
                        for ln, info in c["_per_line"].items():
                            index["executable_lines"].add(ln)
                        index["branches"].update(c["_branches"].keys())
                    if k == "developer_suite":
                        index["configs_developer"][e["config_id"]] = compact_coverage(c)
                res["coverage"] = {k: ({kk: vv for kk, vv in c.items() if not kk.startswith("_")}
                                       if isinstance(c, dict) else c) for k, c in cov.items()}
                if not args.keep_builds:
                    shutil.rmtree(cfg_dir / "sandbox", ignore_errors=True)
                if e["meta"].get("env"):
                    res["env"] = e["meta"]["env"]
            else:
                res["tests"] = None
                res["coverage"] = None
            results.append(res)
            (ctx.results_dir / "summary.partial.json").write_text(json.dumps(
                {"run_id": ctx.run_id, "completed": len(results), "configurations": results}, indent=1))
            if not args.quiet:
                print("   " + one_line(res), flush=True)
        if not args.keep_builds:
            shutil.rmtree(bdir, ignore_errors=True)

    skipped = [{"config_id": e["config_id"], "requested": e["requested"], "errors": e["report"]["errors"]}
               for e in entries if not e["report"]["valid"] and not args.force_invalid]
    agg = aggregate(results, cumulative, len(groups), skipped)
    index_out = {"run_id": ctx.run_id,
                 "executable_lines": sorted(index["executable_lines"]),
                 "branches": sorted(index["branches"]),
                 "configs": index["configs"],
                 "configs_developer": index["configs_developer"],
                 "configurations": {r["config_id"]: r["configuration"] for r in results}}
    (ctx.run_dir / "coverage_index.json").write_text(json.dumps(index_out, indent=1))
    write_run_table(ctx, results)
    if not args.keep_builds:
        shutil.rmtree(ctx.run_dir / "builds", ignore_errors=True)
    summary = {
        "run_id": ctx.run_id,
        "label": args.label,
        "started_utc": started.isoformat(timespec="seconds"),
        "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "command": " ".join([Path(sys.argv[0]).name] + (argv if argv is not None else sys.argv[1:])),
        "subject": {k: csm.data["system"].get(k) for k in ("name", "repository", "commit")},
        "csm": str(csm.path),
        "tools": tool_versions(),
        "sanitizers": {"enabled": not args.no_sanitize, "note": sanitizer_note},
        "run_directory": ctx.rel(ctx.run_dir),
        "coverage_index": ctx.rel(ctx.run_dir / "coverage_index.json"),
        "coverage_table": ctx.rel(ctx.run_dir / "coverage_table.md"),
        "aggregate": agg,
        "rejected_configurations": skipped,
        "configurations": results,
    }
    ctx.results_dir.mkdir(parents=True, exist_ok=True)
    summary["results_file"] = str(ctx.results_dir / "summary.json")
    text = json.dumps(summary, indent=2)
    (ctx.results_dir / "summary.json").write_text(text)
    (ctx.run_dir / "summary.json").write_text(text)
    (ctx.results_dir / "summary.partial.json").unlink(missing_ok=True)
    if not args.quiet:
        print_report(summary)
    bad = agg["tests"]["failed"] + agg["tests"]["crashed"] + agg["build_failures"]
    return 1 if bad else 0


def write_run_table(ctx: Ctx, results: list) -> None:
    hdr = ["| Config | Source | Build | Tests (pass/fail/crash/no-oracle/skip) | Statement | Branch | Function "
           "| Dev-suite stmt | Non-default settings |", "|---|---|---|---|---|---|---|---|---|"]
    csv = ["config_id,source,compile_group,passed,failed,crashed,no_oracle,skipped,stmt_pct,branch_pct,func_pct,"
           "dev_stmt_pct,dev_branch_pct,dev_func_pct"]
    for r in results:
        t = r.get("tests") or {}
        cov = r.get("coverage") or {}
        al, dv = cov.get("all_tests") or {}, cov.get("developer_suite") or {}
        g = lambda d, k: (d.get(k) or {}).get("percent")  # noqa: E731
        nd = ", ".join(f"{k}={v!r}" for k, v in r["non_default_settings"].items()) or "-"
        src = r.get("provenance", {}).get("source", "-")
        hdr.append(f"| {r['config_id']} | {src} | {r['compile_group']}{'' if r['build']['rebuilt'] else ' (reused)'} | "
                   f"{t.get('passed', '-')}/{t.get('failed', '-')}/{t.get('crashed', '-')}/{t.get('no_oracle', '-')}/"
                   f"{t.get('skipped', '-')} | {g(al, 'statement')} | {g(al, 'branch')} | {g(al, 'function')} | "
                   f"{g(dv, 'statement')} | {nd} |")
        csv.append(",".join(str(x) for x in [r["config_id"], src, r["compile_group"], t.get("passed"), t.get("failed"),
                                             t.get("crashed"), t.get("no_oracle"), t.get("skipped"),
                                             g(al, "statement"), g(al, "branch"), g(al, "function"),
                                             g(dv, "statement"), g(dv, "branch"), g(dv, "function")]))
    (ctx.run_dir / "coverage_table.md").write_text("\n".join(hdr) + "\n")
    (ctx.run_dir / "coverage_table.csv").write_text("\n".join(csv) + "\n")


def non_default(csm: vc.CSM, cfg: dict) -> dict:
    return {k: v for k, v in cfg.items() if k in csm.options and csm.options[k]["default_value"] != v}


def one_line(res: dict) -> str:
    if res["tests"] is None:
        return f"{res['config_id']}: BUILD FAILED ({len(res['build']['errors'])} errors)"
    t = res["tests"]
    cov = (res["coverage"] or {}).get("all_tests") or {}
    st = cov.get("statement", {}).get("percent")
    br = cov.get("branch", {}).get("percent")
    fn = cov.get("function", {}).get("percent")
    return (f"{res['config_id']}: pass={t['passed']} fail={t['failed']} crash={t['crashed']} "
            f"no_oracle={t['no_oracle']} skipped={t['skipped']} | stmt={st}% branch={br}% func={fn}% "
            f"| runtime={res['runtime']}")


def aggregate(results: list, cumulative: dict, n_groups: int, skipped: list) -> dict:
    tot = Counter()
    for r in results:
        if r["tests"]:
            for k in ("total", "passed", "failed", "crashed", "no_oracle", "not_run", "skipped", "build_failed"):
                tot[k] += r["tests"][k]
    cov_vals = [r["coverage"]["all_tests"]["statement"]["percent"] for r in results
                if r.get("coverage") and "all_tests" in r["coverage"]]
    fn_vals = [r["coverage"]["all_tests"]["function"]["percent"] for r in results
               if r.get("coverage") and "all_tests" in r["coverage"]]
    cum_total = len(cumulative)
    cum_hit = sum(1 for v in cumulative.values() if v > 0)
    return {
        "configurations_requested": len(results) + len(skipped),
        "configurations_tested": len(results),
        "configurations_rejected": len(skipped),
        "builds": n_groups,
        "build_failures": sum(1 for r in results if r["build"]["status"] != "success" and r["build"]["rebuilt"]),
        "tests": dict(tot),
        "statement_coverage_per_config": {
            "min": min(cov_vals) if cov_vals else None,
            "max": max(cov_vals) if cov_vals else None,
        },
        "function_coverage_per_config": {
            "min": min(fn_vals) if fn_vals else None,
            "max": max(fn_vals) if fn_vals else None,
        },
        "cumulative_statement_coverage": {
            "covered": cum_hit, "total": cum_total,
            "percent": round(100.0 * cum_hit / cum_total, 2) if cum_total else None,
            "note": "Union over all tested configurations of ini.c lines executed / lines executable in at least one build.",
        },
    }


def print_report(s: dict) -> None:
    a = s["aggregate"]
    print("\n=== build_and_test summary ===")
    print(f"subject: {s['subject']['name']} @ {s['subject']['commit'][:12]}   run: {s['run_id']}")
    print(f"configurations: {a['configurations_tested']} tested, {a['configurations_rejected']} rejected, "
          f"{a['builds']} build(s), {a['build_failures']} build failure(s)")
    t = a["tests"]
    print(f"test cases: {t.get('total', 0)} total | pass {t.get('passed', 0)} | fail {t.get('failed', 0)} | "
          f"crash {t.get('crashed', 0)} | no-oracle {t.get('no_oracle', 0)} | skipped {t.get('skipped', 0)}")
    for r in s["configurations"]:
        cov = r.get("coverage") or {}
        d, al = cov.get("developer_suite"), cov.get("all_tests")
        if d and al:
            print(f"  {r['config_id']}: statement {d['statement']['percent']}% "
                  f"({d['statement']['covered']}/{d['statement']['total']}) developer suite -> "
                  f"{al['statement']['percent']}% with generated tests; branch "
                  f"{d['branch']['percent']}% -> {al['branch']['percent']}%; function "
                  f"{d['function']['percent']}% -> {al['function']['percent']}%")
    c = a["cumulative_statement_coverage"]
    if c["total"]:
        print(f"cumulative statement coverage across configurations: {c['percent']}% ({c['covered']}/{c['total']})")
    print(f"summary written to {s['results_file']}; per-config table {s['coverage_table']}")


if __name__ == "__main__":
    sys.exit(main())
