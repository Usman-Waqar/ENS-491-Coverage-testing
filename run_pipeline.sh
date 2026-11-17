#!/usr/bin/env bash
# Reproduce the whole pilot pipeline on inih from a clean state.
#
#   ./run_pipeline.sh                 # outputs in pipeline_out/
#   OUT=/tmp/x ./run_pipeline.sh      # elsewhere
#   ACTS_JAR=/path/acts_3.3.jar ./run_pipeline.sh   # use NIST ACTS instead of the built-in generator
#
# Inputs that are NOT regenerated (human/agent-authored): csm.yaml (reference model),
# discover/reference_aliases.yaml, phase4/coverage_criteria.md and the Phase 4 agent answer
# phase4/iterations/01/response.yaml.
set -euo pipefail
cd "$(dirname "$0")"
OUT=${OUT:-pipeline_out}
if [[ ! -f ../subject_system/ini.c ]]; then
    echo "subject system missing: run 'python3 setup_subject.py' first" >&2
    exit 1
fi
rm -rf "$OUT"
mkdir -p "$OUT"
log() { printf '\n=== %s\n' "$*"; }
latest() { ls -d "$1"/runs/* | tail -1; }

log "0. unit tests"
python3 -m pytest -q tests | tail -1

log "1a. reference model: schema + self-test, JSON view"
python3 csm_tool.py check csm.yaml > "$OUT/model_check.json"
python3 csm_tool.py yaml2json csm.yaml -o csm.json
python3 validate_constraints.py --stats > "$OUT/space_stats.json"

log "1b. discovery pipeline (static S1-S3, S5-S7) and RQ1 score"
python3 discover/run_discovery.py --root ../subject_system --sources ini.h ini.c --headers ini.h \
    --harness csm.yaml --reference csm.yaml --aliases discover/reference_aliases.yaml --out "$OUT/discovery"

log "2. ACTS bridge: export, developer-suite interaction coverage, generation, import, verification"
mkdir -p "$OUT/acts"
python3 acts_bridge.py export --csm csm.json -o "$OUT/acts/inih_t2" > /dev/null
python3 acts_bridge.py coverage --map "$OUT/acts/inih_t2.acts.map.json" developer \
    -o "$OUT/acts/coverage_developer_suite.json" > /dev/null
if [[ -n "${ACTS_JAR:-}" ]]; then
    python3 acts_bridge.py run --map "$OUT/acts/inih_t2.acts.map.json" --jar "$ACTS_JAR" -o "$OUT/acts/inih_t2.acts.csv"
    CSV="$OUT/acts/inih_t2.acts.csv"
else
    python3 acts_bridge.py generate --map "$OUT/acts/inih_t2.acts.map.json" -o "$OUT/acts/inih_t2.builtin.csv" > /dev/null
    CSV="$OUT/acts/inih_t2.builtin.csv"
fi
python3 acts_bridge.py import --map "$OUT/acts/inih_t2.acts.map.json" --csv "$CSV" -o "$OUT/acts/inih_t2.configs.json"
python3 acts_bridge.py coverage --map "$OUT/acts/inih_t2.acts.map.json" "$OUT/acts/inih_t2.configs.json" \
    -o "$OUT/acts/coverage_array.json" > /dev/null
python3 validate_constraints.py --config-file "$OUT/acts/inih_t2.configs.json" --quiet | grep -c ": VALID" \
    | xargs -I{} echo "{} configurations valid"

log "3. build / test / coverage: developer variants, then the covering array"
python3 build_and_test.py --developer-variants --results-dir "$OUT/results_developer" \
    --label "developer variants (tests/meson.build)" --quiet
python3 build_and_test.py --configs "$OUT/acts/inih_t2.configs.json" --results-dir "$OUT/results_array" \
    --label "covering array (pairwise + 3-way groups), seeded with developer variants" --quiet
DEV=$(latest "$OUT/results_developer")
CA=$(latest "$OUT/results_array")
python3 analyze_coverage.py "$CA" > "$OUT/analysis_summary.json"

log "4. Phase 4 iteration 01 from the recorded agent answer, then iteration 02 (convergence check)"
export PHASE4_ITER_ROOT="$OUT/phase4"
mkdir -p "$PHASE4_ITER_ROOT/01"
python3 phase4/loop.py prepare --iter 1 --runs "$CA" --dev-run "$DEV" > /dev/null
cp phase4/iterations/01/response.yaml "$PHASE4_ITER_ROOT/01/response.yaml"
python3 phase4/loop.py ingest --iter 1 --model "claude-opus-5-5 (in-session Phase 4 agent)" > /dev/null
python3 phase4/loop.py execute --iter 1
python3 phase4/loop.py evaluate --iter 1 --runs "$CA" --dev-run "$DEV" > "$OUT/phase4_iter01_summary.json"
IT1=$(latest "$PHASE4_ITER_ROOT/01/results")
python3 phase4/loop.py prepare --iter 2 --runs "$CA" "$IT1" --dev-run "$DEV" > "$OUT/phase4_iter02_prepare.json"

log "5. results tables (RESULTS.md)"
python3 make_results.py --out-dir "$OUT" -o "$OUT/RESULTS.md"
[[ "$OUT" == "pipeline_out" ]] && cp "$OUT/RESULTS.md" RESULTS.md

log "done"
python3 - "$OUT" <<'EOF'
import json, sys
from pathlib import Path
out = Path(sys.argv[1])
cmp_ = json.loads((out / "discovery/compare_static.json").read_text())
dev = json.loads((out / "acts/coverage_developer_suite.json").read_text())
arr = json.loads((out / "acts/coverage_array.json").read_text())
ev = json.loads((out / "phase4/01/evaluation.json").read_text())
p2 = json.loads((out / "phase4_iter02_prepare.json").read_text())
c = ev["coverage"]
print(f"RQ1 static discovery: options P/R {cmp_['options']['precision']}/{cmp_['options']['recall']}, "
      f"active_when acc {cmp_['field_accuracy']['active_when']}, constraints recall {cmp_['constraints']['recall']}, "
      f"interaction pairs P/R {cmp_['interaction_pairs']['precision']}/{cmp_['interaction_pairs']['recall']}")
print(f"RQ2 developer suite t-way interaction coverage: {dev['total']['percent']}% "
      f"(pairwise {dev['relations']['R1']['percent']}%); covering array: {arr['total']['percent']}% "
      f"with {arr['configurations']} configurations")
for k in ("developer_suite", "before", "after"):
    m = c[k]
    print(f"coverage {k:16s}: stmt {m['statement']['percent']}%  branch {m['branch']['percent']}%  "
          f"func {m['function']['percent']}%  ({m['configurations']} configs)")
print(f"RQ3 LLM tests: {ev['tests']['valid']}/{ev['tests']['proposed']} valid; failure-revealing "
      f"{ev['tests']['failure_revealing']}; region branch gaps {len(ev['region_branch_gaps']['before'])} -> "
      f"{len(ev['region_branch_gaps']['after'])}")
print(f"loop converged after iteration 01: {p2['converged']}")
EOF
