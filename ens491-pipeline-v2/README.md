# CIT pipeline for configurable systems: pilot on inih

This is the ENS 491 Project 516 tooling for configuration-space discovery, covering arrays
(NIST ACTS), build/test/coverage automation and LLM-driven test generation. It runs on the pilot
subject [benhoyt/inih](https://github.com/benhoyt/inih) at commit `611ab51` (checkout in `../subject_system/`).

## Setup

Needs **GCC 9 or newer** (for `gcov --json-format`), **git**, and **Python 3.10 or newer**.

```bash
pip install -r requirements.txt
python3 setup_subject.py                # fetches inih @ 611ab51 into ../subject_system
```

**Platform notes:**
- **Linux:** recommended; this is the platform the results come from.
- **Windows:** use **WSL (Ubuntu)**. Native MinGW GCC ships no AddressSanitizer or
  UndefinedBehaviorSanitizer libraries (`cannot find -lasan`). `build_and_test.py` detects this
  and falls back to coverage-only runs, with a warning in the summary, but memory errors then go
  undetected. `run_pipeline.sh` also needs a bash shell (WSL or Git Bash).
- **macOS:** the default `gcc` is clang, whose gcov does not produce the JSON format. Install GCC
  (Homebrew `gcc`) and set `"compiler"` in `csm.yaml` to that binary.

```bash
./run_pipeline.sh                       # everything, from scratch, about 90 s; outputs in pipeline_out/
ACTS_JAR=/path/to/acts.jar ./run_pipeline.sh   # use NIST ACTS Advanced instead of the built-in generator
python3 -m pytest -q tests              # 30 unit tests
```

## Pilot results (inih, `pipeline_out/`)

**Full tables with plain-language explanations: [RESULTS.md](RESULTS.md).**

| | Statement | Branch outcomes | Functions | Configurations |
|---|---|---|---|---|
| Developer suite (16 variants in `tests/meson.build`, developer drivers) | 94.24% | 80.65% | 100% | 13 builds |
| + covering array (pairwise + 3-way high-risk groups) and runtime driver | 97.12% | 90.32% | 100% | 84 (48 builds) |
| + LLM tests, Phase 4 iteration 01 | **100%** | **94.35%** | 100% | 94 |

- **RQ1, static discovery only (S1–S3, S5):**
  - options: 18 of 18 found, 18 correct (precision and recall 1.0);
  - activation conditions: 6 of 6 correct;
  - integer lower bounds: 4 of 4 correct;
  - constraints: 0 of 3 recovered. inih's constraints live in comments, which is the LLM stage's job.
- **RQ2, developer suite interaction coverage:** the developer suite covers **34.7%** of the feasible pairs and 32.9% of all required t-way tuples.
- **RQ3, generated tests:**
  - **12 of 12** LLM tests valid;
  - they close 5 of the 8 branch gaps inside high-risk regions; the other 3, plus 4 more outside those regions, are dead code under every valid configuration;
  - **3 failure-revealing tests**, triaged in `phase4/iterations/01/findings.md`;
  - the feedback loop **converges** after one iteration.

## Layout by phase

| Phase | File | Purpose |
|---|---|---|
| 1 Model | `csm.yaml` | **Human-edited source of truth** (CSM v2): 18 options, 5 constraints, 8 interaction groups |
| | `csm.json` | JSON view generated from it (`csm_tool.py yaml2json`); default input of all tools |
| | `csm_schema.json` | JSON Schema (draft 2020-12) for CSM v2; v1 is archived in `schemas/` |
| | `csm_model.py` | Loader and normaliser; expression language (parse, evaluate, three-valued, NNF) |
| | `csm_tool.py` | `yaml2json`, `json2yaml`, `migrate` (v1→v2), `check`, `summary` |
| | `validate_constraints.py` | Validate configurations; partition mapping; inert options; `--stats`, `--self-test` |
| 1 Discovery | `discover/cpp_scan.py` | S1: options, defaults and types from `#ifndef/#define`; activation conditions via Boolean difference + minimisation; interaction-group candidates; `#error` constraints; comment leads |
| | `discover/build_scan.py` | S2: meson/CMake/autoconf options, macro aliases (incl. inverted ones), masking, documentation hits |
| | `discover/runtime_scan.py` | S3: runtime-option seeds (alternative entry points, callback contracts, getenv/getopt/argparse) |
| | `discover/llm_enrich.py` | S4: chunked prompt for an LLM agent; guarded ingestion (G1 structure, G2 names, G3 evidence, G4 values) |
| | `discover/verify.py` | S5: `gcc -E` equivalence for activation conditions; sanitizer probes for integer lower bounds; build/run probes for hard constraints |
| | `discover/merge.py`, `compare.py` | S6: merge into v2 model; S7: precision/recall against the reference model (semantic constraint matching) |
| | `discover/run_discovery.py` | Runs S1–S7 |
| 2 ACTS | `acts_bridge.py` | `export` (ACTS text input + token map), `run` (ACTS jar), `generate` (built-in generator, cost-aware two-level), `import` (CSV → configs, cost-aware fill), `coverage` (independent t-way checker) |
| 3 Pipeline | `build_and_test.py` | Groups configurations by build; coverage build + ASan/UBSan build; developer drivers + runtime driver; line/branch/function coverage, per-function tables, per-test-case coverage, run index; cleanup |
| | `analyze_coverage.py` | Which settings trigger which code paths; test-case-aware executed-interaction coverage; gap list |
| | `drivers/csm_driver.c`, `drivers/alloc_shim.c`, `generated_tests/` | Runtime-configurable driver (differential oracle); allocator shim; 9 generated inputs |
| 4 LLM loop | `phase4/coverage_criteria.md` | Criteria the LLM optimises (C0 validity gate, C1–C5) |
| | `phase4/loop.py` | `prepare` (gap report + prompt) → `ingest` (validated tests, fault-injection drivers, constraint hypotheses) → `execute` → `evaluate` (V1–V5, before/after, convergence) |
| | `phase4/iterations/01/` | `prompt.md` (input), `response.yaml` (agent output), `findings.md` (triage of failure-revealing tests) |
| Docs | `docs/phase1_2_design.md` | Phase 1–2 design note |

## Key commands

```bash
python3 csm_tool.py check csm.yaml && python3 csm_tool.py yaml2json csm.yaml        # after editing the model
python3 validate_constraints.py --config '{"INI_USE_STACK": 0, "INI_INITIAL_ALLOC": 12}'
python3 discover/run_discovery.py --root ../subject_system --sources ini.h ini.c --headers ini.h \
        --harness csm.yaml --reference csm.yaml --aliases discover/reference_aliases.yaml --out out/disc \
        [--llm-response answer.yaml --model "<id>"]                                 # RQ1 run
python3 acts_bridge.py export --csm csm.json -o out/inih_t2                         # [--relations none] [--seed none] [--soft as-hard]
python3 acts_bridge.py coverage --map out/inih_t2.acts.map.json developer           # RQ2: developer-suite interaction coverage
python3 acts_bridge.py generate --map out/inih_t2.acts.map.json -o out/a.csv        # or: run --jar acts.jar -o out/a.csv
python3 acts_bridge.py import --map out/inih_t2.acts.map.json --csv out/a.csv -o out/configs.json
python3 build_and_test.py --configs out/configs.json --results-dir out/results
python3 analyze_coverage.py out/results/runs/<run_id>
python3 phase4/loop.py prepare --iter 3 --runs <run dirs> --dev-run <developer run>
```

## Policies and definitions

- **Inert options.** An option whose `active_when` is false is equivalent to its default: the
  same build, verified by `gcc -E`. The bridge adds `if not(active_when) then opt == default`, so a
  pair such as (`INI_USE_STACK=1`, `INI_INITIAL_ALLOC=5`) is infeasible instead of falsely
  "covered" (configuration-level masking).
- **Soft constraints** (C2, C3, C4) are legal but contradictory. They are tested and warned about,
  not excluded (`--soft as-hard` excludes them).
- **High-risk groups** become ACTS `[Relation]` entries at their suggested strength (3). Measured
  cost on inih: pairwise alone needs about 26 builds; the 3-way groups raise that to 48, because
  `G_heap_buffer` and `G_continuation` each need at least 32 compile-time rows.
- **Executed interaction coverage.** A tuple counts only if a configuration with that tuple
  executed a *discriminating* line of the group's region: one that isn't executed in every
  configuration. Tuples whose region is compiled out are not applicable (test-case-aware CIT,
  Yilmaz 2013). The metric is coarse for regions that span complementary `#if` branches;
  code-level gaps (C2) are the sharper signal.

## Limitations

- **ACTS not run yet.** ACTS Advanced has not been run: it is obtained from NIST by e-mail, and
  this environment cannot reach csrc.nist.gov. The export follows the ACTS 3.3 user guide (Nov
  2023), and `acts_bridge.py coverage` verifies any CSV that ACTS produces.
- **Reference model is provisional.** `csm.yaml` was built in an agent session with empirical
  checks. The team must review it before RQ1 scores are reported against it.
- **S4 not run as an experiment.** It needs a fresh session or another model; the session that
  wrote the reference would be contaminated. `tests/fixtures/s4_response_fixture.yaml` only
  exercises the guards.
- **Phase 4 iteration 01 was produced by the same agent session.** Its findings need team
  confirmation before they are reported upstream.
