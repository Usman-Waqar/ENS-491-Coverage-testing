# Coverage criteria for LLM-generated tests (Phase 4)

These criteria are the objective the LLM is asked to optimise. Every criterion has a metric
that the scripts compute, so a test's contribution is measured, not claimed.

## C0 – Validity (gate)

A generated test counts only if **all** of these hold. They are checked by `phase4/loop.py evaluate`.

| Check | Meaning | How it is verified |
|---|---|---|
| V1 constraint-valid | Its configuration satisfies every hard constraint of the CSM. Soft-constraint violations are allowed, but they are reported. | `validate_constraints.validate` at ingestion |
| V2 builds | Library and driver compile for that configuration (coverage build and ASan/UBSan build). | `build_and_test.py` build status |
| V3 runs | The test input is processed. No sanitizer report, crash or timeout is attributable to the harness. | case present in driver output |
| V4 has an oracle | `differential` (entry points must agree), `contains` (strings expected in the callback trace) or `lines` (exact expected trace). | oracle verdict recorded |
| V5 hits its target | The test executes every line and branch outcome it declares as a target. | per-test-case coverage (`per_case_coverage.json`) |

If a valid test's oracle fails, or a sanitizer reports an error in library code, the test is a
**failure-revealing test**. It must be inspected by a person before anyone calls it a defect
(proposal §3.3, fourth criterion).

## C1 – Executed interaction coverage (primary objective)

For every high-risk interaction group in `csm.yaml`, each feasible t-way tuple of its options
(t = `suggested_strength`) must be exercised by at least one valid test case. "Exercised" means
the test runs, in a configuration containing that tuple, at least one *discriminating* line of
the group's code region: a region line that is not executed in every configuration.

Tuples whose region code is compiled out in that build are *not applicable*. Configuration-level
coverage (some configuration has the tuple) is necessary but not sufficient, following
test-case-aware CIT (Yilmaz 2013).

Metric: `analysis/interactions.json`, `percent_executed`.

Secondary goal: depth of at least 2 distinct test cases per tuple (`tuples_exercised_by_one_case_only` goes down).

## C2 – Branch coverage inside interaction regions

Every branch outcome inside a high-risk region should be taken in at least one configuration.
These are the places where an interaction fault would show.

Metric: never-taken branch outcomes whose line lies in a group region (`gap_report.json`, `region_branch_gaps`).

## C3 – Boundary coverage of discretised options

For integer options, a test should hit the class boundaries of the configured value. For
example, with `INI_MAX_LINE = M`, input lines of length M−3, M−2 and M−1 (the buffer needs 3
spare bytes), and for the realloc path, lines that force 1, 2 or several doublings.

Metric: count of valid tests whose target kind is `boundary`, per integer option.

## C4 – Global structural coverage

Statement, branch and function coverage of the system under test, unioned over all tested
configurations. Targets are the lines never executed and branch outcomes never taken in any
configuration.

A target that cannot be reached by input plus configuration must be classified, with a reason:
- `needs_fault_injection` (e.g. malloc returning NULL), or
- `unreachable` (e.g. the false branch of `assert(p != NULL)` when callers never pass NULL).

Metric: `summary.json` aggregate and `analysis/gaps.json`.

## C5 – Cost

Prefer configurations whose compile-time part already exists in the run, listed as "existing
builds" in the prompt: those tests cost no rebuild. A test may introduce a new compile-time
configuration only when its target requires it.

Metric: new distinct builds per iteration.

## Priority

C0 is a gate. Above it the order is **C1 > C2 > C3 > C4**, with C5 as the tie-breaker.

## Constraint mining (same iteration)

Alongside tests, the LLM proposes constraint hypotheses: invalid or contradictory option
combinations, and edge cases the covering array did not exclude or explore. Each needs evidence
(file and lines). Hard hypotheses are probed by building and running a violating configuration
under sanitizers. Soft ones are kept as documented hypotheses. Nothing enters `csm.yaml`
without status `confirmed`.
