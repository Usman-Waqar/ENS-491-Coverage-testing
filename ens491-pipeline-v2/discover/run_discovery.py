#!/usr/bin/env python3
"""Run the configuration-space discovery pipeline (S1-S7) end to end.

  python3 discover/run_discovery.py --root ../subject_system \
      --sources ini.h ini.c --headers ini.h --harness csm.yaml \
      --reference csm.yaml --aliases discover/reference_aliases.yaml \
      --out discover_out [--llm-response answer.yaml --model "<id>"]

Without --llm-response the run is static-only (S1, S2, S3, S5, S6, S7) and
writes the S4 prompt files to <out>/prompts/ for an LLM-based agent. With a
response, S4 ingests it, S5 re-verifies the new elements and S6/S7 produce
the enriched model and its score.

Artifacts in <out>/: s1_cpp.json s2_build.json s3_runtime.json view.json
s5_verify.json csm_static.yaml prompts/ [s4_llm.json s5_verify_llm.json
csm_discovered.yaml] compare_*.json compare_*.md
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import build_scan  # noqa: E402
import compare  # noqa: E402
import cpp_scan  # noqa: E402
import csm_model as cm  # noqa: E402
import csm_tool  # noqa: E402
import llm_enrich  # noqa: E402
import merge  # noqa: E402
import runtime_scan  # noqa: E402
import verify  # noqa: E402


def _w(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, default=list))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--root", required=True)
    ap.add_argument("--sources", nargs="+", required=True)
    ap.add_argument("--headers", nargs="+", required=True)
    ap.add_argument("--harness", required=True)
    ap.add_argument("--reference")
    ap.add_argument("--aliases")
    ap.add_argument("--out", required=True)
    ap.add_argument("--llm-response")
    ap.add_argument("--model", default="unknown-llm")
    ap.add_argument("--skip-bounds", action="store_true")
    a = ap.parse_args(argv)

    root = Path(a.root).resolve()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    raw_h = cm.read_model_file(a.harness)
    harness = {"system": raw_h["system"], "build": raw_h["build"], "test_suite": raw_h["test_suite"]}
    hcsm = cm.load_csm(a.harness)
    timings = {}

    t = time.time()
    s1 = cpp_scan.scan(root, a.sources)
    _w(out / "s1_cpp.json", s1)
    timings["S1"] = time.time() - t
    t = time.time()
    s2 = build_scan.scan(root, [o["name"] for o in s1["options"]])
    _w(out / "s2_build.json", s2)
    timings["S2"] = time.time() - t
    t = time.time()
    s3 = runtime_scan.scan(root, a.headers, [s for s in a.sources if s not in a.headers])
    _w(out / "s3_runtime.json", s3)
    timings["S3"] = time.time() - t

    view = merge.build_view(s1, s2, s3, None)
    _w(out / "view.json", view)
    t = time.time()
    s5 = verify.verify(view, root, {"build": hcsm.data["build"], "test_suite": raw_h["test_suite"]},
                       hcsm.path.parent.resolve(), a.sources[-1] if a.sources[-1].endswith(".c") else "ini.c",
                       a.skip_bounds)
    _w(out / "s5_verify.json", s5)
    timings["S5"] = time.time() - t
    static_model = merge.build_model(s1, s2, s3, None, s5, harness)
    cm.schema_validate(static_model)
    (out / "csm_static.yaml").write_text(csm_tool.dump_yaml(static_model, "# static-only discovery (S1-S3, S5)\n"))
    prompts = llm_enrich.prepare(root, s1, s2, s3, out / "csm_static.yaml", out / "prompts")
    print(f"static model: {len(static_model['options'])} options, {len(static_model['constraints'])} constraints, "
          f"{len(static_model['interaction_groups'])} groups; prompts: {[p.name for p in prompts]}")

    final = out / "csm_static.yaml"
    if a.llm_response:
        s4 = llm_enrich.ingest(root, out / "csm_static.yaml", Path(a.llm_response).read_text(), a.model,
                               a.sources)
        _w(out / "s4_llm.json", s4)
        print(f"S4 ingest: {s4.get('stats', s4.get('error'))}")
        view2 = merge.build_view(s1, s2, s3, s4)
        s5b = verify.verify(view2, root, {"build": hcsm.data["build"], "test_suite": raw_h["test_suite"]},
                            hcsm.path.parent.resolve(), "ini.c", skip_bounds=True)
        s5b["bounds"] = s5["bounds"]
        _w(out / "s5_verify_llm.json", s5b)
        model = merge.build_model(s1, s2, s3, s4, s5b, harness)
        cm.schema_validate(model)
        final = out / "csm_discovered.yaml"
        final.write_text(csm_tool.dump_yaml(model, f"# discovery S1-S6 with LLM enrichment ({a.model})\n"))

    if a.reference:
        aliases = cm.read_model_file(a.aliases).get("options", {}) if a.aliases else {}
        ref = cm.load_csm(a.reference)
        for label, path in [("static", out / "csm_static.yaml")] + \
                           ([("llm", final)] if final != out / "csm_static.yaml" else []):
            rep = compare.compare(ref, cm.load_csm(path), aliases)
            _w(out / f"compare_{label}.json", rep)
            (out / f"compare_{label}.md").write_text(compare.to_markdown(rep, f"RQ1 score: {label} discovery vs reference"))
            print(f"[{label}] options P/R {rep['options']['precision']}/{rep['options']['recall']}  "
                  f"constraints P/R {rep['constraints']['precision']}/{rep['constraints']['recall']} "
                  f"(+hyp {rep['constraints_incl_hypotheses']['precision']}/{rep['constraints_incl_hypotheses']['recall']})  "
                  f"pairs P/R {rep['interaction_pairs']['precision']}/{rep['interaction_pairs']['recall']}  "
                  f"active_when acc {rep['field_accuracy']['active_when']}")
    _w(out / "timings.json", {k: round(v, 2) for k, v in timings.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
