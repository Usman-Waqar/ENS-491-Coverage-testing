# Pilot results: inih (ENS 491 Project 516)

**Run:** 17 Nov 2026 on inih @ `611ab51`; reproduce with `workspace/run_pipeline.sh`.

**Caveat:** the numbers are from one pilot subject and one in-session agent. The reference model
and the Phase 4 findings still need team review.

## Subject and model

- **Library:** inih, a small INI parser in C: `ini.c` has 139 executable lines in the union of builds, 124 branch outcomes and 10 functions.
- **Model:** 16 compile-time options (`#ifndef/#define` macros) and 2 runtime options (API entry point, callback return policy).
- **Space:** 47.2M discretised configurations, 35.4M valid, 450,560 distinct builds once options with no effect are collapsed.
- **Constraints:** 1 hard (C1) and 4 soft. C4 was mined by the LLM loop and confirmed by a test; C5 is still a hypothesis.

## RQ1: configuration-space discovery (static stages only, vs the provisional reference model)

| Element | Precision | Recall | Notes |
|---|---|---|---|
| Options | 1.0 | 1.0 | 16 compile-time options from S1; 2 runtime candidates from S3, mapped by a human alias file |
| Activation conditions (`active_when`) | – | 6/6 correct | Boolean difference over `#if` structure; all confirmed by `gcc -E` equivalence (S5) |
| Integer lower bounds | – | 4/4 correct | ASan/UBSan probes at 0–3 (S5) |
| Integer partitions | – | 3/4 identical | heuristic classes around default and probed bound |
| Build-system aliases | – | 15/15 | including the inverted `use_heap` ↔ `INI_USE_STACK` and the `distro_install` masking |
| Constraints | – | 0/3 | inih states its constraints in comments only; the comment leads that carry them are in the S4 prompt |
| Interaction pairs | 1.0 | 0.36 | mechanical groups from `#if` nesting vs reference groups from code reading |

## RQ2: what the developer suite tests

The 16 developer test variants (13 distinct builds) cover:

- **Pairwise:** 34.7% of the 1,209 feasible pairs.
- **High-risk 3-way groups:**

| Group | Covered |
|---|---|
| `G_comment_prefixes` | 12.5% |
| `G_continuation` | 12.8% |
| `G_reader_buffer` | 20% |
| `G_heap_buffer` | 32.8% |
| `G_error_flow` | 33.3% |
| `G_no_value` | 33.3% |
| `G_custom_alloc` | 80% |

The covering array (84 configurations, 48 builds, seeded with the developer variants) reaches
100% of the 1,433 required tuples. Running it took 50 s: 3,532 test-case verdicts, no failures,
no sanitizer reports.

## RQ3: coverage and failures

| Test set | Statement | Branch outcomes | Functions |
|---|---|---|---|
| Developer suite | 94.24% | 80.65% | 100% |
| + covering array and runtime driver | 97.12% | 90.32% | 100% |
| + 12 LLM tests (iteration 01) | 100% | 94.35% | 100% |

- **Validity:** 12 of 12 generated tests valid (built, ran, have an oracle, hit every declared target). Two of them use an LLM-written fault-injection allocator.
- **Branch gaps inside high-risk regions:** 8 → 3.
- **Remaining gaps:** all 7 are dead code under every valid configuration:
  - assert false branches;
  - `*start == 0` after the comment check;
  - `!chars` in the inline-comments-off build;
  - `num < 2`, which needs out-of-range sizes.
- **Convergence:** the loop converges after one iteration.

### Failure-revealing tests (preliminary triage; team confirmation pending)

1. **F1, robustness:** an overlong line whose discarded tail starts with a NUL byte makes inih silently drop the *next* line. Reproduced with the default build.
2. **F2, interaction `INI_ALLOW_MULTILINE` × `INI_MAX_NAME`:** continuation lines of names longer than `INI_MAX_NAME-1` are reported under a truncated name (`longname_x=first`, then `longnam=second`). The header documentation says names are truncated; the first callback's name is not.
3. **F3, configuration pitfall:** `INI_MAX_NAME=1` (legal) silently disables multi-line continuation. It is now soft constraint C4.

None of the three is caught by the developer suite.

## Next steps

1. Request ACTS Advanced from NIST and run `ACTS_JAR=… ./run_pipeline.sh`, then compare its array size with the built-in generator's.
2. Team review of `csm.yaml`, making it the RQ1 ground truth.
3. Run S4 in a fresh session and with a second model, and score RQ1 with constraints.
4. Confirm F1–F3, then report upstream.
5. Pick the next subjects. The tools assume C and `#ifdef` options; S2 already reads CMake and autoconf declarations.
