# Phase 1–2 Design: Configuration Space Model and NIST ACTS Bridge

ENS 491 Project 516, design note, 17 Nov 2026. Pilot subject: inih @ `611ab51`. Covers Phase 1 (model schema + discovery) and Phase 2 (ACTS covering arrays + translation into runnable configurations). Phases 3–4 build on the interfaces fixed here.

## 0. Design principles

1. **One model, two views.** People author and review the model in YAML (`csm.yaml`). Tools consume the same content as JSON (`csm.json`). One JSON Schema (draft 2020-12) validates both, because YAML is loaded into the same data structure before validation.
2. **Every claim is checkable.** Each option, setting and constraint carries evidence (file + lines) and provenance (who or what proposed it, and what confirmed it). This is what lets RQ1 measure precision and recall against the hand-built reference model, and what filters out hallucinated options.
3. **ACTS is the generator, not the oracle.** We never trust a covering array blindly. Our own t-way checker re-verifies every array ACTS produces against the model's constraints. The same checker measures interaction coverage of the developer suite (Objective 3).
4. **Cost awareness lives in our layer.** ACTS has no notion of rebuild cost, so the bridge fills don't-care cells and orders rows to minimise distinct builds (after Demiröz and Yilmaz [2]).

## 1. Phase 1: configuration space model (CSM v2)

### 1.1 What changes from the current `csm.json` (v1)

The v1 model already captures binding time, discrete settings, defaults, evidence, constraints and activation conditions. v2 keeps all of that and adds what Phases 2 and 4 need:

| Need | v1 | v2 |
|---|---|---|
| Human-readable authoring | JSON only | YAML source, generated JSON |
| Integer ranges | flat list of representative values | **interval partitions**: every legal value maps to exactly one class with one representative |
| Values ACTS can't express (`";#"`, `""`) | none | per-setting `token` (ACTS-safe identifier), auto-generated if omitted |
| Default with no code default (e.g. a callback's return policy) | implicit | `default_source: code | docs | build_system | convention` |
| Discovery provenance | free-text method | per-element `provenance` (static / llm / human) and `status` (hypothesis / confirmed / refuted) |
| High-risk interaction hints | none | `interaction_groups`: option sets sharing code, with a suggested strength t; these feed ACTS `[Relation]` |
| Constraint origin | rationale text | `origin: documented | code | empirical | llm` plus `status` |

v1 tools (`validate_constraints.py`, `build_and_test.py`) keep working through a loader that normalises v2 to the v1 fields they use.

### 1.2 Schema structure

```yaml
csm_version: "2.0"
system:            # name, repository, commit, language, local_path, sut_sources
build:             # compiler, sources, cflags, coverage/sanitizer flags   (unchanged)
test_suite:        # drivers, developer variants, oracles                 (unchanged)

options:           # list of option records, see 1.3
constraints:       # list of constraint records, see 1.4
interaction_groups:  # list of high-risk groups, see 1.5
excluded_options:  # variability found but left out, with reasons
```

### 1.3 Option record

```yaml
- name: INI_MAX_LINE                      # [A-Za-z_][A-Za-z0-9_]*  (also a legal ACTS parameter name)
  binding: compile_time                   # compile_time | runtime
  description: Line buffer size in bytes (3 more than the longest line).
  domain:
    type: integer                         # boolean | enum | integer | string
    range: { min: 2, max: 2147483647 }    # legal values; required for integer
    partitions:                           # required for integer and string; optional for enum
      - { label: degenerate, interval: [2, 2],       representative: 2 }
      - { label: small,      interval: [3, 199],     representative: 20 }
      - { label: default,    interval: [200, 200],   representative: 200 }
      - { label: large,      interval: [201, "max"], representative: 4096 }
    rationale: Classes relative to input line length L — always overflows,
      overflows on some test lines, project default, never overflows.
  default: 200                            # REQUIRED for every option
  default_source: code                    # code | docs | build_system | convention
  active_when: "INI_USE_STACK == 1 or INI_ALLOW_REALLOC == 1"   # optional; inert otherwise
  apply: { mechanism: c_macro, macro: INI_MAX_LINE, format: int }
  evidence:
    - { file: ini.h, lines: "138-142", kind: definition }
    - { file: ini.c, lines: "100-101,146-166", kind: use }
    - { file: README.md, lines: "35", kind: documentation }
  provenance:
    discovered_by: static                 # static | llm | human
    model: null                           # LLM id when discovered_by == llm
    confirmed_by: [preprocess_equivalence, sanitizer_probe]
    status: confirmed                     # hypothesis | confirmed | refuted
```

Rules the schema enforces:

- **Every option has a `default`**, and the default must fall inside some partition, or be one of the enum or boolean values.
- **The settings used for test generation are the partition representatives** (or all values for boolean/enum). Any concrete value, such as a developer variant's `INI_INITIAL_ALLOC=12`, maps to its class. This replaces v1's "legal but off-partition" warning with a precise mapping, which Phase 2 needs to seed ACTS with the developer suite.
- **Booleans are 0/1** in the model, matching C preprocessor semantics. The bridge renders them as `true`/`false` for ACTS.
- **Enum and string settings may carry a `token`.** For example `{ value: ";#", token: SEMI_HASH }`. If omitted, the bridge derives one: an identifier-safe form of the label, falling back to `V0, V1, ...`.
- **Runtime options** use `apply.mechanism: driver_cli | env_var | config_file | api_argument`. Compile-time options must use `c_macro` (or `cmake_option` / `configure_flag` for later subjects).

### 1.4 Constraint record

```yaml
- id: C1
  kind: hard                     # hard: configuration invalid | soft: legal but contradictory
  expr: "if INI_CUSTOM_ALLOCATOR == 1 then INI_USE_STACK == 0"
  origin: documented             # documented | code (#error, static_assert) | empirical | llm
  status: confirmed
  rationale: ini.h states the custom allocator requires the heap buffer.
  evidence: [{ file: ini.h, lines: "190-191", kind: documentation }]
```

The expression language is unchanged from v1: `if … then …`, `and/or/not`, `== != < <= > >=`, `in [..]`, `not in [..]`, and option-to-option comparisons. The parsed AST is generated, not hand-written, so the human text is the single source of truth.

### 1.5 Interaction groups (high-risk hints)

```yaml
- id: G_heap_buffer
  options: [INI_USE_STACK, INI_ALLOW_REALLOC, INI_INITIAL_ALLOC, INI_MAX_LINE]
  reason: shared_code            # shared_code | nested_ifdef | shared_function | documented
  evidence: [{ file: ini.c, lines: "99-161", kind: use }]
  suggested_strength: 3
```

This operationalises the proposal's definition of "high-risk" (options that jointly control the same code, §3.1.2). Discovery derives groups mechanically from `#if` nesting and from macros used in the same function. Phase 2 turns each group into an ACTS `[Relation]` at the suggested strength.

### 1.6 Discovery module

A pipeline of independent stages. Each stage reads and writes a partial CSM, so stages can be run, compared and ablated separately for RQ1.

```
discover/
  extract_cpp.py       S1  static: preprocessor directives
  extract_build.py     S2  static: meson_options.txt, CMake option(), configure.ac, Makefile -D
  extract_runtime.py   S3  static seeds: getopt/getopt_long tables, argparse, config-struct fields
  enrich_llm.py        S4  LLM agent: semantics, partitions, runtime options, constraint hypotheses
  verify.py            S5  empirical confirmation / refutation
  merge.py             S6  union with provenance -> csm.yaml
  compare.py           S7  precision/recall vs the hand-built reference model (RQ1)
```

**S1 – preprocessor extractor** (pure Python, no dependencies). This stage tokenises `#if/#ifdef/#ifndef/#elif/#else/#endif/#define/#error` across headers and sources and keeps a condition stack, which gives:

| Pattern found | What it yields |
|---|---|
| `#ifndef X` / `#define X <lit>` | option `X` with its **default**; type inferred from the literal (`0/1` → boolean, integer → integer, `"..."` → string) |
| `#if X`, `#ifdef X`, uses of `X` in code | use locations |
| `X` used only under `#if Y` | **presence condition**, which becomes the `active_when` candidate |
| `#if A && B` / nested `#if A` … `#if B` | co-location, which becomes an `interaction_groups` candidate |
| `#error` under a condition | **hard constraint** with `origin: code` |

On inih, S1 alone should recover all 16 compile-time options with their defaults. It should also recover all six activation conditions in the v1 model, because each one is visible in the `#if` structure of `ini.c`:

- `INI_MAX_NAME` is used only under `#if INI_ALLOW_MULTILINE` (line 110).
- `INI_MAX_LINE` is used under `#if INI_USE_STACK` (line 99) and under `#if INI_ALLOW_REALLOC && !INI_USE_STACK` (line 145). The union of those two conditions is `INI_USE_STACK || INI_ALLOW_REALLOC`.

S1 also surfaces the nested `#if INI_ALLOW_MULTILINE` / `#if INI_ALLOW_INLINE_COMMENTS` block (lines 189–201) as an interaction-group candidate. These are exactly the facts that took manual effort in the v1 extraction.

**S2 – build-system extractor.** This stage reads `meson_options.txt` and its CMake and autoconf equivalents, which yields aliases (`use_heap` ↔ `INI_USE_STACK`, inverted), declared choices and ranges, and **masking rules**. For example, meson's `distro_install=true` ignores every feature option.

**S3 – runtime seeds.** Runtime options are the hard part (proposal §2.1). The static pass only collects candidates: `getopt` option strings, `struct option[]` tables, `argparse.add_argument`, config-key string literals, and public API parameters. They are handed to S4 as leads, not as model entries.

**S4 – LLM enrichment** (Claude Code as the primary agent; other models for cross-checking). The agent receives the S1–S3 candidates and **chunked** source and documentation excerpts (proposal §5, risk 4). For each option it proposes a description, partitions with rationale, constraints, and any runtime options. Guards:

- Output must validate against the schema. Invalid output goes back to the agent with the validator error.
- **Evidence must check out:** the cited file exists, the lines exist, and they mention the option name. Otherwise the element is dropped and counted as a hallucination for RQ1.
- Everything the LLM adds enters with `status: hypothesis`.

**S5 – empirical verification** (automating what was done by hand for v1):

| Check | Method | Confirms |
|---|---|---|
| Inertness | `gcc -E` of the source with and without the option varied under the candidate condition; identical output means inert | `active_when` |
| Range bounds | build at boundary − 1, boundary and boundary + 1 with ASan/UBSan and run the developer suite | `range.min/max` |
| Hard constraints | build and run each violating combination; build failure, link failure or sanitizer report confirms it | `kind: hard` |
| Soft constraints | differential run: behaviour differs from what the documentation says the option does | `kind: soft` |

S5 promotes `hypothesis` → `confirmed` or `refuted`. Refuted elements stay in the file, marked, so the evaluation can count them.

**S7 – comparison for RQ1.** Options are matched by name. Settings are matched by value or by partition class. **Constraints are matched semantically:** two constraint sets are equivalent if they accept exactly the same configurations of the discretised space. This is checked by enumeration on the variables the constraints mention, which `validate_constraints.py --stats` already does. Textual matching would penalise correct but differently written constraints.

## 2. Phase 2: NIST ACTS bridge

### 2.1 ACTS facts the design depends on

From the NIST ACTS user guide (Basic 1.0 / Advanced 3.3, Nov 2023):

- **Constraints, mixed strength and `[Relation]` require the Advanced version.** That version is obtained by emailing NIST with your name and organisation. The Basic jar is a direct download but has no constraint support. ACTS is public domain.
- **Command line:** `java -Ddoi=<t> -Dalgo=ipog -Dmode=scratch|extend -Dchandler=forbiddentuples|solver|no -Doutput=csv -Dcheck=on -Drandstar=on|off -jar acts.jar <in.txt> <out>`. `-Dalgo=null` only checks the coverage of an existing test set. `-Ddoi=-1` enables mixed strength.
- **Input sections:** `[System]` (`Name: …`), `[Parameter]` (`name (boolean|enum|int) : v1, v2, …`), `[Relation]` (`R1 : (p1, p2, …, t)`), `[Constraint]`, `[Test Set]` (a header line of parameter names, then comma-separated rows; `*` means don't care). Comment lines start with `--`.
- **Value restrictions:** parameter names follow Java identifier rules. Values may not contain comma, semicolon, double quote or backslash.
- **Constraint grammar:**
  - Relational operators: `= != > < >= <=`.
  - Boolean operators: `&& || =>`, in precedence order `&&`, `||`, `=>`.
  - Enum values go in double quotes; booleans are `true` / `false`.
  - `>`, `<` and arithmetic apply only to numeric parameters.
  - **The guide lists no negation operator.**
- **CSV output:** `#` comment lines (including `# Degree of interaction coverage: t`), a header row of parameter names, then one row per configuration.

### 2.2 Data flow

```
csm.yaml ──yaml2json──▶ csm.json
                            │
         acts_bridge.py export (t, policies)
                            ▼
        inih.acts.txt  +  inih.acts.map.json   (token ↔ value, name ↔ option)
                            │
         acts_bridge.py run  →  java … -jar acts.jar
                            ▼
                     inih.acts.csv
                            │
         acts_bridge.py import (decode, fill *, validate, order)
                            ▼
   configs.json  ─────────▶ build_and_test.py --configs   (Phase 3)
        │
        └─ acts_bridge.py coverage → interaction_coverage.json (independent t-way check)
```

### 2.3 Export: CSM → ACTS input

**Parameters.** Each option becomes one ACTS parameter, and its generation settings become the value list:

| CSM domain | ACTS type | Values |
|---|---|---|
| boolean | `boolean` | `false, true` (0/1) |
| integer | `int` | partition representatives |
| enum / string | `enum` | setting tokens (`SEMI_HASH`, `NONE`, …) |

The token map is written next to the input file and is the only way back from ACTS output to concrete values.

**Constraint translation.** This is a recursive rewrite of the constraint AST:

1. `if A then B` → `(A) => (B)`. Every sub-expression is fully parenthesised, so ACTS precedence never matters.
2. **Negation is pushed down to the atoms**, because the guide lists no `!`. This uses De Morgan's laws on `and/or` and flips the operators (`==`↔`!=`, `<`↔`>=`, `<=`↔`>`). `not (A => B)` becomes `A && !B`, then normalised the same way.
3. `x in [a, b]` → `(x = a || x = b)`. `x not in [a, b]` → `(x != a && x != b)`.
4. Literals: booleans → `true/false`; enums → `"TOKEN"`; integers stay as they are.
5. Option-to-option comparisons (`INI_INITIAL_ALLOC <= INI_MAX_LINE`) are emitted directly for `int` parameters. For non-numeric parameters they are expanded into a disjunction over value pairs.

**Policies** (command-line flags; the defaults are recommendations):

| Policy | Options | Default | Why |
|---|---|---|---|
| `--soft` | `omit` / `as-hard` | `omit` | Soft-violating configurations (C2, C3) build and run. They are where documented meaning and behaviour diverge, so they are worth testing. `as-hard` gives a "semantically clean" space for comparison runs. |
| `--inert` | `collapse` / `free` | `collapse` | Adds `(not active_when) => opt = default` for each option. A pair such as (`INI_USE_STACK=1`, `INI_INITIAL_ALLOC=5`) is then infeasible rather than "covered" by a row in which `INI_INITIAL_ALLOC` does nothing. This is configuration-level masking in the sense of Yilmaz [8]; without it, coverage numbers are inflated. |
| `--relations` | `none` / `auto` | `auto` | Emits one base relation over all parameters at strength t, plus one relation per `interaction_group` at its `suggested_strength`. Switches ACTS to `-Ddoi=-1`. |
| `--seed` | `none` / `developer` | `developer` | Writes the developer variants as a `[Test Set]` and runs ACTS in `extend` mode (§2.5). |

**Example output for inih** (abridged; generated from the v1 model with the default policies):

```
-- acts_bridge export: inih @ 611ab51, t=2, soft=omit, inert=collapse, relations=auto
[System]
Name: inih

[Parameter]
INI_HANDLER_LINENO (boolean) : false, true
INI_ALLOW_MULTILINE (boolean) : true, false
INI_START_COMMENT_PREFIXES (enum) : SEMI_HASH, SEMI, HASH, NONE
INI_ALLOW_INLINE_COMMENTS (boolean) : true, false
INI_INLINE_COMMENT_PREFIXES (enum) : SEMI, HASH, SEMI_HASH
INI_USE_STACK (boolean) : true, false
INI_MAX_LINE (int) : 2, 20, 200, 4096
INI_MAX_NAME (int) : 1, 8, 50, 256
INI_ALLOW_REALLOC (boolean) : false, true
INI_INITIAL_ALLOC (int) : 2, 5, 20, 200
INI_CUSTOM_ALLOCATOR (boolean) : false, true
api_entry_point (enum) : ini_parse, ini_parse_file, ini_parse_stream, ini_parse_string, ini_parse_string_length
handler_return_policy (enum) : reject_flagged, accept_all, reject_all
-- … 5 more parameters

[Relation]
R1 : (INI_HANDLER_LINENO, INI_ALLOW_MULTILINE, …, handler_return_policy, 2)
R2 : (INI_USE_STACK, INI_ALLOW_REALLOC, INI_INITIAL_ALLOC, INI_MAX_LINE, 3)

[Constraint]
-- C1 (hard, documented)
(INI_CUSTOM_ALLOCATOR = true) => (INI_USE_STACK = false)
-- inert collapse (active_when)
(INI_ALLOW_INLINE_COMMENTS = false) => (INI_INLINE_COMMENT_PREFIXES = "SEMI")
(INI_ALLOW_MULTILINE = false) => (INI_MAX_NAME = 50)
(INI_USE_STACK = true) => (INI_ALLOW_REALLOC = false)
(INI_USE_STACK = true) => (INI_INITIAL_ALLOC = 200)
((INI_USE_STACK = false) && (INI_ALLOW_REALLOC = false)) => (INI_MAX_LINE = 200)
```

The collapse rule for `INI_CUSTOM_ALLOCATOR` is the contrapositive of C1, so it is deduplicated.

**Size.** The pairwise model has 18 parameters and 1,219 raw value pairs before infeasible pairs are removed. The two largest domains (5 × 4) give a lower bound of 20 rows. The 3-way relation R2 adds 2 × 2 × 4 × 4 = 64 required triples, minus infeasible ones, so the final count will be somewhat above 20. This compares with 35.4M valid configurations.

### 2.4 Run

`acts_bridge.py run` is a thin wrapper. It locates the jar (`$ACTS_JAR` or `--jar`), builds the `-D` flags from the export metadata, uses `-Doutput=csv`, `-Dcheck=on` and `-Drandstar=off`, and records the ACTS version, command line and run time in the result.

`-Drandstar=off` keeps don't-care cells as `*`, so our layer fills them deterministically (§2.6) instead of ACTS picking random values. That gives reproducibility and lower build cost.

### 2.5 Seeding with the developer suite (the bridge to RQ2)

Each developer variant becomes one `[Test Set]` row. Its compile-time settings are mapped to partition classes, unspecified options take their defaults, and runtime options take the driver's fixed runtime values (`unittest` → `ini_parse` / `reject_flagged`; `unittest_string` → `ini_parse_string` / `accept_all`). This gives three outputs from one model:

1. `-Dalgo=null` on the seeds gives the **t-way interaction coverage of the existing developer suite**. Our checker independently recomputes it.
2. `-Dmode=extend` gives the **rows ACTS must add**. These are, by construction, the configurations that exercise interactions the developer suite never reaches: the Stage 2 gap list.
3. Restricting extend mode to the `interaction_groups` relations gives the **high-risk** subset of that gap. This is the input to Phase 4's LLM test generation.

Caveat, to carry into Phase 3: a seed row "covers" a pair at configuration level only. Whether a test actually executes the code where the two options interact is a test-case-level question [8]. Phase 3's per-configuration line and branch data is used to discount pairs whose shared code region (from `interaction_groups` evidence) was never executed.

### 2.6 Import: ACTS output → executable configurations

1. **Parse.** Skip `#` lines; the first remaining line is the header. Check that the header matches the exported parameter list exactly, and fail loudly if not.
2. **Decode.** Map tokens back to CSM values through `*.map.json`.
3. **Fill don't-cares** (`--fill cost-aware`, the default):
   - Runtime `*` → default.
   - Compile-time `*` → the value that makes the row's effective compile-time key equal to a key already in use, provided the row stays valid; otherwise the default.
   - Fewer distinct keys means fewer rebuilds. The alternative `--fill default` exists for ablation.
4. **Validate.** Run every row through `validate_constraints.py`. A hard violation means ACTS and our model disagree. That is a bridge bug, reported and never silently dropped.
5. **Order.** Group rows by effective compile-time key and sort, so `build_and_test.py` builds each key once.
6. **Emit** `configs.json`, a list of configuration dicts, with a sidecar `provenance` entry per row:

```json
{ "config_id": "acts-t2-007",
  "source": "acts",                     
  "acts": { "row": 7, "doi": 2, "algo": "ipog", "mode": "extend", "jar": "acts_3.3.jar" },
  "seed_variant": null,                 
  "filled_dont_cares": ["INI_ALLOW_BOM"],
  "compile_group": "61cb925678",
  "configuration": { "INI_USE_STACK": 0, "INI_ALLOW_REALLOC": 1, "...": "..." } }
```

`build_and_test.py --configs configs.json` already accepts this list (extra keys are ignored by v1 and become first-class in Phase 3).

### 2.7 Independent coverage checker

`acts_bridge.py coverage configs.json --t 2 [--relations auto]` reports `covered / required` t-tuples, overall and per relation, plus the list of uncovered tuples.

- A t-tuple is **required** only if it is feasible: some configuration of the discretised space contains it and satisfies all hard constraints (plus collapse rules when `--inert collapse` is on).
- Feasibility is decided by backtracking search over the discretised domains, pruning on the constraint formulas. This is fast here: 18 parameters, at most 5 values each.

This serves three purposes:

1. It verifies ACTS output, so we don't assume ACTS is correct.
2. It measures the developer suite's interaction coverage for RQ2 (proposal Objective 3: "computed with our own scripts").
3. It measures the coverage gain of LLM-generated tests in Phase 4.

### 2.8 Until the Advanced jar arrives

`acts_bridge.py generate --engine builtin` provides a small constraint-aware greedy pairwise generator (IPOG-style, using the same validator) behind the same interface.

- It keeps Phases 3–4 unblocked.
- It is clearly labelled `source: builtin` in provenance.
- Once ACTS is available it is still useful as a cross-check: same model, two generators, both verified by §2.7.

Reported results will use ACTS.

## 3. Open decisions

1. **Authoring format:** YAML source plus generated JSON (recommended), or keep hand-edited JSON.
2. **Soft constraints in arrays:** `omit` (recommended; test the contradictions) or `as-hard`.
3. **Inert collapse:** on by default (recommended; prevents inflated coverage).
4. **Seeding with developer variants:** on by default (recommended; yields the RQ2 gap list directly).
5. **ACTS Advanced:** someone on the team should email NIST now (name plus "Sabancı University", mentioning a request for the ACTS tool). Until it arrives, the builtin generator stands in.

## 4. Implementation order (next step)

1. `csm_schema.json` v2, `csm.yaml` for inih, `yaml2json`, and a v1-compatible loader.
2. `acts_bridge.py export`, with golden-file tests for constraint translation (negation push-down, `in`, option-to-option).
3. `acts_bridge.py coverage`, then `import`; then the builtin generator.
4. `discover/extract_cpp.py` and `verify.py`, checked by regenerating the inih model and diffing it against the hand-built v1 model. That diff is the first RQ1 data point.

## References

- NIST, *ACTS User Guide (Basic 1.0 and Advanced 3.3)*, Nov 2023. https://csrc.nist.gov/csrc/media/Projects/automated-combinatorial-testing-for-software/documents/acts_user_guide_for_basic1.0_and_advanced3.3_versions_nov23.pdf
- NIST, *Downloadable Tools: Automated Combinatorial Testing for Software*. https://csrc.nist.gov/projects/automated-combinatorial-testing-for-software/downloadable-tools
- [2], [8]: numbering as in the ENS 491 proposal (Demiröz & Yilmaz 2012; Yilmaz 2013).

## 5. Implementation status (17 Nov 2026)

All four phases are implemented and run end to end on inih (`./run_pipeline.sh`, about 90 s). Results are in `docs/pilot_results_inih.md`. Where the code differs from the design above:

- **Schema details:**
  - `provenance.discovered_by` is a list (the union after merging), with `stages`, `confirmed_by` and `refuted_by`.
  - String and enum options use `values` with ACTS `token`s; interval partitions are for integer options only.
  - Interaction groups have a `region` (file and lines), which Phases 3–4 use to check that a test really executed the interaction.
- **Inert-collapse rules are deduplicated semantically.** The rule for `INI_CUSTOM_ALLOCATOR` is equivalent to C1, and the exporter detects that by enumeration, not by text.
- **Built-in generator, cost-aware mode.** `generate --strategy two-level` covers compile-time tuples first, then packs runtime tuples onto existing builds. On inih it needs 48 builds instead of 53 for the same 100% coverage. The 3-way relations dominate the build count: pairwise alone needs about 26.
- **Coverage checker.** A tuple is required only if some valid configuration contains it, found by backtracking with three-valued constraint evaluation. The checker re-verifies every array, ACTS output included.
- **S4 has not been run as an experiment.** The session that built the reference model would contaminate it. The prompt bundle (`pipeline_out/discovery/prompts/`) is ready for a fresh session or another model, and a synthetic fixture tests the guards.
- **Beyond the design:**
  - per-test-case coverage;
  - setting-to-code attribution;
  - test-case-aware executed-interaction coverage;
  - fault-injection support drivers in Phase 4;
  - loop convergence detection.
