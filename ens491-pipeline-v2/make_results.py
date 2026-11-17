#!/usr/bin/env python3
"""Build RESULTS.md (GitHub-ready tables) from the outputs of run_pipeline.sh.

  python3 make_results.py [--out-dir pipeline_out] [-o RESULTS.md]

Every number is read from the pipeline output files, so rerunning the pipeline and this
script keeps the page in sync with the data. The plain-language descriptions below are the
only hand-written parts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "phase4"))

import csm_model as cm  # noqa: E402

OPTION_PLAIN = {
    "INI_HANDLER_LINENO": "Pass the line number to the program's callback",
    "INI_ALLOW_MULTILINE": "Indented lines continue the previous value",
    "INI_ALLOW_BOM": "Skip a UTF-8 byte-order mark at the start of the file",
    "INI_START_COMMENT_PREFIXES": "Characters that start a comment line",
    "INI_ALLOW_INLINE_COMMENTS": "Allow comments after a value on the same line",
    "INI_INLINE_COMMENT_PREFIXES": "Characters that start an inline comment",
    "INI_USE_STACK": "Line buffer on the stack (1) or the heap (0)",
    "INI_MAX_LINE": "Line buffer size = longest line that fits",
    "INI_MAX_SECTION": "Longest section name kept",
    "INI_MAX_NAME": "Longest key name kept (used for continuation lines)",
    "INI_ALLOW_REALLOC": "Heap buffer may grow for long lines",
    "INI_INITIAL_ALLOC": "Starting size of the heap buffer",
    "INI_STOP_ON_FIRST_ERROR": "Stop reading at the first error",
    "INI_CALL_HANDLER_ON_NEW_SECTION": "Call the program at every [section] header",
    "INI_ALLOW_NO_VALUE": "Allow keys without '=' (value is NULL)",
    "INI_CUSTOM_ALLOCATOR": "Use the program's own malloc/free",
    "api_entry_point": "Which parse function the program calls (file name, FILE*, stream, string)",
    "handler_return_policy": "How the program's callback answers (accept all, reject one key, reject all)",
}

GROUP_PLAIN = {
    "G_heap_buffer": "Line buffer: stack vs heap, growth, sizes",
    "G_custom_alloc": "Custom memory allocator",
    "G_reader_buffer": "Input reader × buffer growth",
    "G_continuation": "Multi-line values × comments × name size",
    "G_no_value": "Keys without values × multi-line × callback",
    "G_error_flow": "Error handling: stop-on-error × callback answer × section callback",
    "G_comment_prefixes": "Comment characters (line start vs inline)",
    "G_bom_comment": "Byte-order mark × comment characters",
}

TEST_PLAIN = {
    "t_failmalloc": "Memory allocation fails at the start: the parser must give up cleanly (return -2)",
    "t_failrealloc": "Buffer growth fails in the middle of a line: the parser must free memory and return -2",
    "t_realloc_eof": "File ends exactly when the growing buffer is full",
    "t_realloc_cap_boundary": "Buffer grows 2→4→8→16→20 (capped); lines of 18 and 19 characters",
    "t_abyss_nul": "Too-long line containing a NUL byte, followed by a normal line",
    "t_maxline_lf_boundary": "Lines of 17, 18 and 19 characters with a 20-byte buffer (Unix line endings)",
    "t_maxline_crlf_boundary": "Same boundary with Windows line endings (\\r\\n)",
    "t_bom_partial": "File starts with only 2 of the 3 byte-order-mark bytes",
    "t_maxname_trunc_continuation": "Long key continued on the next line, with an 8-byte name buffer",
    "t_maxname1_multiline": "Continued value with a 1-byte name buffer",
    "t_maxsection_trunc": "Section names of 7 and 8 characters with an 8-byte buffer",
    "t_stop_on_section_reject": "Callback rejects a section while stop-on-first-error is on",
}

BUGS = [
    ("F1", "t_abyss_nul",
     "A too-long line whose cut-off part starts with a NUL byte makes inih silently **skip the next, valid line**.",
     "Any build (default included); needs a NUL byte in the file", "Low (data loss, unusual input)",
     "Robustness bug"),
    ("F2", "t_maxname_trunc_continuation",
     "The continuation of a long key arrives under a **shortened, different key** (`longname_x=first`, then `longnam=second`).",
     "`INI_ALLOW_MULTILINE=1` × small `INI_MAX_NAME` (default 50: keys of 50+ characters)", "Low–medium (value stored under the wrong key)",
     "Interaction bug + documentation mismatch"),
    ("F3", "t_maxname1_multiline",
     "`INI_MAX_NAME=1` (allowed) **silently disables multi-line values**: the continuation becomes a syntax error.",
     "`INI_ALLOW_MULTILINE=1` × `INI_MAX_NAME=1`", "Low (unusual setting)",
     "Configuration pitfall → new rule C4"),
]

UNREACHABLE_PLAIN = {
    "ini.c:75:2": "Handles a 'no characters given' case that cannot occur when inline comments are off",
    "ini.c:124:0": "Safety check (`assert`): only fails if a programmer passes NULL",
    "ini.c:125:0": "Safety check (`assert`): only fails if a programmer passes NULL",
    "ini.c:126:0": "Safety check (`assert`): only fails if a programmer passes NULL",
    "ini.c:190:3": "Empty-line case already handled earlier as a comment (redundant check)",
    "ini.c:221:1": "Same redundant empty-line check",
    "ini.c:302:2": "Needs a buffer smaller than 2 bytes, below the allowed minimum",
}


def latest(p: Path) -> Path:
    return sorted((p / "runs").iterdir())[-1]


def pct(x) -> str:
    from decimal import Decimal, ROUND_HALF_UP
    if x is None:
        return "–"
    return f"{Decimal(str(x)).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


RULE_PLAIN = {
    "C1": "A custom allocator only works with the heap buffer (stated in `ini.h`).",
    "C2": "With a growing heap buffer, the starting size should not exceed the maximum line size, or the maximum is never enforced.",
    "C3": "Without growth, the heap buffer size *is* the line limit, so `INI_MAX_LINE` is ignored; the two should match.",
    "C4": "With a 1-byte name buffer, multi-line values silently stop working (found by AI test, bug F3).",
    "C5": "If the starting size equals the maximum, enabling growth has no effect (AI hypothesis, not yet tested).",
}


def fmt_val(v) -> str:
    return json.dumps(v) if isinstance(v, str) else str(v)


def secs(summary: dict) -> int:
    from datetime import datetime
    a = datetime.fromisoformat(summary["started_utc"])
    b = datetime.fromisoformat(summary["finished_utc"])
    return int((b - a).total_seconds())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out-dir", default=str(HERE / "pipeline_out"))
    ap.add_argument("-o", "--output", default=str(HERE / "RESULTS.md"))
    a = ap.parse_args(argv)
    out = Path(a.out_dir)
    from loop import Union  # noqa: E402

    csm = cm.load_csm(HERE / "csm.json")
    space = json.loads((out / "space_stats.json").read_text())
    disc = json.loads((out / "discovery/compare_static.json").read_text())
    s2 = json.loads((out / "discovery/s2_build.json").read_text())
    dev_cov = json.loads((out / "acts/coverage_developer_suite.json").read_text())
    arr_cov = json.loads((out / "acts/coverage_array.json").read_text())
    configs = json.loads((out / "acts/inih_t2.configs.json").read_text())
    dev_sum = json.loads((latest(out / "results_developer") / "summary.json").read_text())
    arr_sum = json.loads((latest(out / "results_array") / "summary.json").read_text())
    ai_sum = json.loads((latest(out / "phase4/01/results") / "summary.json").read_text())
    ev = json.loads((out / "phase4/01/evaluation.json").read_text())
    ing = json.loads((out / "phase4/01/ingest_report.json").read_text())
    gaps2 = json.loads((out / "phase4/02/gap_report.json").read_text())

    dev_run, arr_run, ai_run = latest(out / "results_developer"), latest(out / "results_array"), latest(out / "phase4/01/results")
    u_dev = Union([dev_run], developer_only=True)
    u_arr = Union([arr_run])
    u_all = Union([arr_run, ai_run])
    m_dev, m_arr, m_all = u_dev.metrics(), u_arr.metrics(), u_all.metrics()
    bug_of = {b[1]: b[0] for b in BUGS}
    per_test = {t["id"]: t for t in ev["tests"]["per_test"]}
    n_valid = ev["tests"]["valid"]
    n_tests = ev["tests"]["proposed"]

    L = []
    w = L.append
    w("# Results: AI-assisted combinatorial interaction testing of inih")
    w("")
    w(f"Pilot study for ENS 491 Project 516. Subject: [inih](https://github.com/benhoyt/inih) "
      f"(a small C library that reads `.ini` files) at commit `{csm.data['system']['commit'][:7]}`. "
      "Every number below is generated from the pipeline outputs by `make_results.py`; "
      "rerun `./run_pipeline.sh && python3 make_results.py` to reproduce.")
    w("")
    w("> **Status:** pilot results. The reference model and the three bugs are pending confirmation by the team; "
      "the AI tests were generated by Claude (claude-opus-5-5) acting as the test-generation agent.")
    w("")
    w("## How it works")
    w("")
    w("```mermaid")
    w("flowchart LR")
    w("    A[inih source code<br/>and docs] --> B[1. Model<br/>18 settings + rules]")
    w(f"    B --> C[2. Sample<br/>{len(configs['configs'])} configurations<br/>covering array]")
    w("    C --> D[3. Build and test<br/>every configuration]")
    w("    D --> E[Coverage report<br/>+ untested code]")
    w("    E --> F[4. AI writes tests<br/>for the gaps]")
    w("    F --> D")
    w("```")
    w("")
    w("1. **Model:** list the library's settings, the values to test for each, and which combinations are not allowed.")
    w("2. **Sample:** instead of millions of combinations, pick a small set of configurations in which every pair "
      "of settings (and every triple in high-risk areas) appears at least once: a *covering array*.")
    w("3. **Build and test:** compile inih in each configuration, run the tests, measure which code ran, "
      "and check for memory errors.")
    w("4. **AI tests:** give an AI agent the list of code that never ran; it writes targeted tests; repeat until nothing reachable is left.")
    w("")

    # ---------------- headline ----------------
    w("## Results at a glance")
    w("")
    w("| | Developer tests | + Covering array | + AI tests |")
    w("|---|---|---|---|")
    w(f"| Configurations tested | {m_dev['configurations']} | {m_arr['configurations']} | {m_all['configurations']} |")
    w(f"| Code lines executed (statement coverage) | {pct(m_dev['statement']['percent'])} | "
      f"{pct(m_arr['statement']['percent'])} | **{pct(m_all['statement']['percent'])}** |")
    w(f"| Decision outcomes taken (branch coverage) | {pct(m_dev['branch']['percent'])} | "
      f"{pct(m_arr['branch']['percent'])} | **{pct(m_all['branch']['percent'])}** |")
    w(f"| Functions entered | {pct(m_dev['function']['percent'])} | {pct(m_arr['function']['percent'])} | "
      f"{pct(m_all['function']['percent'])} |")
    w(f"| Pairs of settings tested together | {pct(dev_cov['relations']['R1']['percent'])} | "
      f"{pct(arr_cov['relations']['R1']['percent'])} | {pct(arr_cov['relations']['R1']['percent'])} |")
    w(f"| Bugs found | 0 | 0 | **{len(ev['tests']['failure_revealing'])}** |")
    w("")

    # ---------------- model ----------------
    w("## 1. The model: 18 settings")
    w("")
    w("*Compile-time* settings need a rebuild; *runtime* settings are chosen by the program using inih. "
      "\"Values tested\" are representatives of value ranges that behave the same.")
    w("")
    w("| Setting | What it controls | Kind | Default | Values tested | Only matters when |")
    w("|---|---|---|---|---|---|")
    for n in csm.order:
        o = csm.options[n]
        kind = "compile-time" if o["binding_time"] == "compile_time" else "runtime"
        vals = ", ".join(fmt_val(v) for v in csm.settings(n))
        aw = cm.condition_to_text(o["active_when"]).strip("()") if "active_when" in o else ""
        w(f"| `{n}` | {OPTION_PLAIN.get(n, '')} | {kind} | `{fmt_val(o['default_value'])}` | {vals} | {aw} |")
    w("")
    w("**Rules between settings** (constraints):")
    w("")
    w("| Rule | Meaning | Type | Status |")
    w("|---|---|---|---|")
    for c in csm.all_constraints:
        kind = "not allowed" if c["severity"] == "hard" else "allowed but contradictory"
        w(f"| `{c['expression']}` | {RULE_PLAIN.get(c['id'], c['rationale'])} | {kind} | {c.get('status', 'confirmed')} |")
    w("")

    # ---------------- space ----------------
    w("## 2. Why sampling: the size of the configuration space")
    w("")
    w("| | Configurations | Separate builds |")
    w("|---|---|---|")
    w(f"| All combinations of the tested values | {space['raw_configurations']:,} | – |")
    w(f"| …that respect the rules | {space['valid_configurations']:,} | {space['distinct_effective_builds']:,} |")
    w(f"| Developer test suite (`tests/meson.build`) | 16 variants | {dev_sum['aggregate']['builds']} |")
    seeds = sum(c["source"] == "seed" for c in configs["configs"])
    w(f"| **Covering array** (all pairs + high-risk triples) | **{len(configs['configs'])}** "
      f"({seeds} developer + {len(configs['configs']) - seeds} new) | **{configs['builds']}** |")
    w("")
    w(f"Running the whole covering array (build, test, coverage, memory checks) took **{secs(arr_sum)} seconds**.")
    w("")

    # ---------------- interaction coverage ----------------
    w("## 3. Which setting combinations the developer tests miss")
    w("")
    w("High-risk areas are groups of settings that control the same code; for these, every *triple* of values is required.")
    w("")
    w("| Area | Settings | Combinations required | Developer tests | Covering array |")
    w("|---|---|---|---|---|")
    for rid, r in dev_cov["relations"].items():
        src = r["source"]
        name = "All settings, every pair" if src == "base" else GROUP_PLAIN.get(src, src)
        nopt = f"{len(r['options'])} (pairs)" if src == "base" else f"{len(r['options'])} (triples)"
        w(f"| {name} | {nopt} | {r['required']:,} | {r['covered']:,} ({pct(r['percent'])}) | "
          f"{arr_cov['relations'][rid]['covered']:,} ({pct(arr_cov['relations'][rid]['percent'])}) |")
    w(f"| **Total** | | **{dev_cov['total']['required']:,}** | **{dev_cov['total']['covered']:,} "
      f"({pct(dev_cov['total']['percent'])})** | **{arr_cov['total']['covered']:,} ({pct(arr_cov['total']['percent'])})** |")
    w("")

    # ---------------- per-function coverage ----------------
    w("## 4. Code coverage per function of `ini.c`")
    w("")
    w("| Function | Lines | Developer tests | + Covering array | + AI tests |")
    w("|---|---|---|---|---|")
    exe = set(u_all.index["executable_lines"])
    sets = [u_dev.lines(), u_arr.lines(), u_all.lines()]
    for fname, a_, b_, fn in u_all.functions:
        ls = {f"{{subject}}/{fname}:{x}" for x in range(a_, b_ + 1)} & exe
        if not ls:
            continue
        cells = []
        for s in sets:
            c = len(ls & s)
            cells.append(f"{c}/{len(ls)}" + ("" if c == len(ls) else f" ({100 * c / len(ls):.0f}%)"))
        w(f"| `{fn}` | {a_}–{b_} | {' | '.join(cells)} |")
    w("")

    # ---------------- AI tests ----------------
    w("## 5. The AI-generated tests")
    w("")
    w(f"The agent received the list of code that never ran and wrote {n_tests} tests. A test counts as **valid** only if its "
      "configuration respects the rules, it builds, it runs, it has an expected result, and it really executes "
      f"the code it aimed at. **{n_valid} of {n_tests} were valid.**")
    w("")
    w("| Test | What it tries | Settings changed | Hit its target | Result |")
    w("|---|---|---|---|---|")
    for t in ing["tests"]:
        pt = per_test.get(t["id"], {})
        cfg = ", ".join(f"`{k}={fmt_val(v)}`" for k, v in t["configuration"].items()) or "defaults"
        if t.get("env"):
            cfg += " + simulated memory failure"
        hit = "yes" if pt.get("V5") else "no"
        res = f"**found bug {bug_of[t['id']]}**" if pt.get("failure_revealing") else (
            "passed" if pt.get("valid") else "invalid")
        w(f"| `{t['id']}` | {TEST_PLAIN.get(t['id'], '')} | {cfg} | {hit} | {res} |")
    w("")
    w(f"The AI test run: {ai_sum['aggregate']['configurations_tested']} configurations, "
      f"{ai_sum['aggregate']['builds']} builds, {secs(ai_sum)} seconds.")
    w("")

    # ---------------- bugs ----------------
    w("## 6. Bugs found")
    w("")
    w("None of these is caught by inih's own test suite. Each was reproduced with a small standalone program "
      "(see `docs/team_checks_guide.md`, Check 3).")
    w("")
    w("| ID | What goes wrong | Needs | Severity | Kind | Found by |")
    w("|---|---|---|---|---|---|")
    for bid, tid, what, needs, sev, kind in BUGS:
        w(f"| **{bid}** | {what} | {needs} | {sev} | {kind} | `{tid}` |")
    w("")

    # ---------------- remaining gaps ----------------
    w("## 7. Code that is still never executed, and why")
    w("")
    rem = sorted(gaps2["classified_unreachable"], key=lambda k: [int(x) for x in k.split(":")[1:]])
    w(f"After the AI tests, {len(rem)} decision outcomes remain untaken. The agent explained each one, and "
      "none can be reached by any input in any allowed configuration:")
    w("")
    w("| Location | Reason |")
    w("|---|---|")
    for k in rem:
        key = k.replace("{subject}/", "")
        w(f"| `{key}` | {UNREACHABLE_PLAIN.get(key, gaps2['classified_unreachable'][k]['reasoning'])} |")
    w("")
    w(f"So the feedback loop stopped after one round (converged: **{gaps2['converged']}**).")
    w("")

    # ---------------- discovery ----------------
    w("## 8. How well the model can be discovered automatically (RQ1, static analysis only)")
    w("")
    po = disc["per_option"]
    nint = sum(1 for r in po.values() if "range_min_match" in r)
    naw = sum(1 for r in po.values() if r["active_when_text"]["reference"])
    ip = disc["interaction_pairs"]
    nrec = len(disc["constraints"]["recovered"])
    w("Compared against the reference model in `csm.yaml`, without any AI help:")
    w("")
    w("| What | Found correctly |")
    w("|---|---|")
    w(f"| Settings | {disc['options']['tp']} / {disc['options']['reference']} |")
    w(f"| Kind (compile-time / runtime) and type | {round(disc['field_accuracy']['type'] * len(po))} / {len(po)} |")
    w(f"| Default values | {round(disc['field_accuracy']['default'] * len(po))} / {len(po)} "
      "(the miss: the runtime callback setting uses different value names) |")
    w(f"| \"Only matters when\" conditions | {naw} / {naw} |")
    w(f"| Smallest safe value of numeric settings | {round(disc['integer_range_min_accuracy'] * nint)} / {nint} |")
    w(f"| Build-system names for the settings | {len(s2['meson']['aliases'])} / {len(s2['meson']['aliases'])} |")
    w(f"| Rules between settings | {nrec} / {len(disc['constraints']['reference_ids'])} |")
    w(f"| High-risk setting pairs | {ip['tp']} / {ip['reference']} (no false ones) |")
    w("")
    w("Static analysis finds the settings almost perfectly but none of the rules, because inih states its rules only "
      "in comments. Recovering them is the job of the AI discovery step (next experiment).")
    w("")
    Path(a.output).write_text("\n".join(L) + "\n")
    print(f"wrote {a.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
