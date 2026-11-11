# CIT workspace for inih

Subject system: [benhoyt/inih](https://github.com/benhoyt/inih) at commit `611ab51f8dd7` (2026-11-02), cloned into `../subject_system/`.

## Layout

| Path | What it is |
|---|---|
| `csm_schema.json` | JSON Schema (draft 2020-12) for a Configuration Space Model |
| `csm.json` | The extracted model of inih: 16 compile-time + 2 runtime options, 3 constraints, 6 activation conditions, developer test-suite metadata |
| `validate_constraints.py` | Domain + constraint checker; also the library the harness imports (sampling, effective-config collapsing) |
| `build_and_test.py` | Build / test / coverage runner |
| `drivers/csm_driver.c` | Generated test driver that takes the runtime options on its command line |
| `drivers/alloc_shim.c` | Pass-through `ini_malloc`/`ini_free`/`ini_realloc` so every driver links when `INI_CUSTOM_ALLOCATOR=1` |
| `generated_tests/*.ini` | 9 new inputs aimed at interactions the developer inputs do not reach |
| `results/summary.json` | Latest run; full artefacts per run under `results/runs/<run_id>/` |

Paths inside `csm.json` use two placeholders: `{subject}` (the checkout) and `{workspace}` (this directory). `source_location` paths are relative to the checkout.

## Usage

```bash
# Validate configurations (exit 0 = valid)
python3 validate_constraints.py --default
python3 validate_constraints.py --config '{"INI_USE_STACK": 0, "INI_ALLOW_REALLOC": 1, "INI_INITIAL_ALLOC": 5}'
python3 validate_constraints.py --config-file my_configs.json --quiet
python3 validate_constraints.py --self-test     # schema + formula/expression agreement
python3 validate_constraints.py --stats         # size of the discretised space

# Build, test, measure
python3 build_and_test.py --default
python3 build_and_test.py --config '{"INI_MAX_LINE": 20, "api_entry_point": "ini_parse_string"}'
python3 build_and_test.py --configs sampled.json          # e.g. output of a pairwise generator
python3 build_and_test.py --sample random:40 --seed 7
python3 build_and_test.py --sample each-choice
python3 build_and_test.py --developer-variants            # reproduces tests/unittest.sh
```

Options: `--no-sanitize`, `--strict` (soft constraints become errors), `--force-invalid`, `--timeout`, `--results-dir`, `--quiet`. Exit status is 1 if any test fails, crashes, or a build fails.

## How the harness works

1. **Validate** each configuration. Hard-constraint or domain violations are recorded under `rejected_configurations` and not built.
2. **Group** by the *effective* compile-time settings: options whose `active_when` is false are inert and collapse to their default (verified with `gcc -E`), so e.g. `INI_INITIAL_ALLOC` on the stack build never triggers a rebuild.
3. **Build** per group, twice: a coverage build (`gcc --coverage -fprofile-arcs -ftest-coverage` + `-D` macros) and, unless `--no-sanitize`, an ASan/UBSan build. Coverage is measured on the first; pass/fail/crash verdicts come from the second. Two builds are needed because sanitizer instrumentation changes gcov's line and branch tables (110 vs 113 lines for the default build).
4. **Test** each configuration in a fresh sandbox:
   - developer drivers (`tests/unittest*.c`) with their hard-wired runtime behaviour, judged against the golden baseline of the developer variant whose settings match, split into one verdict per parsed input;
   - `csm_driver` with the configuration's runtime options over the 13 developer inputs and the 9 generated ones. Two oracles: the developer baseline (when `handler_return_policy` is `reject_flagged`) and a differential oracle that re-parses each input through a different entry point and requires an identical callback trace and return code.
5. **Measure** with `gcov -b --json-format` on `ini.c` only, once after the developer drivers and once after everything, and write an lcov tracefile (`*.info`) for each snapshot.

Test case statuses: `pass`, `fail`, `crash` (signal, sanitizer report, failed `assert`, timeout), `no_oracle` (ran cleanly but no applicable oracle), `skipped` (driver incompatible with the configuration), `build_failed`, `not_run`.

## Checks performed

- `csm.json` validates against `csm_schema.json`; `--self-test` reports no problems.
- `--developer-variants` reproduces all 16 developer variants and every golden baseline passes (455 pass, 0 fail).
- `csm_driver` output is byte-identical to `tests/unittest.c` for all five entry points.
- Mutation sanity check (on a copy of the checkout): an inline-comment logic change was caught by the baseline oracle, an off-by-one in `ini_strncpy0` by ASan, and a change to the string reader **only** by the differential oracle.
