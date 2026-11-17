# Phase 4, iteration 01: gap analysis, constraint mining and targeted test generation

You are the test-generation agent of a combinatorial-interaction-testing study of the C library inih (`ini.c`, `ini.h`). The covering array and the existing tests below have been built and run under every listed configuration with gcov and ASan/UBSan. Analyse the remaining gaps, propose constraint hypotheses, and write targeted tests that close the gaps under the criteria.

## Coverage criteria

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


## Current coverage (union over all tested configurations)

- configurations: 84
- statement: 135/139 (97.12%)
- branch outcomes: 112/124 (90.32%)
- functions: 10/10 (100.0%)
- t-way configuration coverage (pairwise + 3-way groups): 1433/1433 (100.0%)
- for comparison, the developer suite alone: statement 94.24%, branch 80.65%, function 100.0%

## Executed interaction coverage (C1)

| Group | t | Required | Config-covered | Executed (of applicable) | Covered but not executed (sample) |
|---|---|---|---|---|---|
| G_heap_buffer | 3 | 61 | 61 | 61 (100.0%) | [] |
| G_custom_alloc | 3 | 5 | 5 | 4 (100.0%) | [] |
| G_reader_buffer | 3 | 40 | 40 | 40 (100.0%) | [] |
| G_continuation | 3 | 78 | 78 | 78 (100.0%) | [] |
| G_no_value | 3 | 12 | 12 | 12 (100.0%) | [] |
| G_error_flow | 3 | 12 | 12 | 12 (100.0%) | [] |
| G_comment_prefixes | 3 | 16 | 16 | 16 (100.0%) | [] |
| G_bom_comment | 2 | 8 | 8 | 8 (100.0%) | [] |

## Never-taken branch outcomes (12); those inside high-risk regions are C2 targets

### `{subject}/ini.c:75:2` in `ini_find_chars_or_comment` (regions: G_comment_prefixes)
```c
   72          s++;
   73      }
   74  #else
   75>     while (*s && (!chars || !strchr(chars, *s))) {
   76          s++;
   77      }
   78  #endif
```
### `{subject}/ini.c:124:0` in `ini_parse_stream`
```c
  121      char abyss[16];  /* Used to consume input when a line is too long. */
  122      size_t abyss_len;
  123  
  124>     assert(reader != NULL);
  125      assert(stream != NULL);
  126      assert(handler != NULL);
  127  
```
### `{subject}/ini.c:125:0` in `ini_parse_stream`
```c
  122      size_t abyss_len;
  123  
  124      assert(reader != NULL);
  125>     assert(stream != NULL);
  126      assert(handler != NULL);
  127  
  128  #if !INI_USE_STACK
```
### `{subject}/ini.c:126:0` in `ini_parse_stream`
```c
  123  
  124      assert(reader != NULL);
  125      assert(stream != NULL);
  126>     assert(handler != NULL);
  127  
  128  #if !INI_USE_STACK
  129      line = (char*)ini_malloc(INI_INITIAL_ALLOC);
```
### `{subject}/ini.c:130:0` in `ini_parse_stream` (regions: G_heap_buffer, G_custom_alloc)
```c
  127  
  128  #if !INI_USE_STACK
  129      line = (char*)ini_malloc(INI_INITIAL_ALLOC);
  130>     if (!line) {
  131          return -2;
  132      }
  133  #endif
```
### `{subject}/ini.c:152:0` in `ini_parse_stream` (regions: G_heap_buffer, G_custom_alloc, G_reader_buffer)
```c
  149              if (max_line > INI_MAX_LINE)
  150                  max_line = INI_MAX_LINE;
  151              new_line = ini_realloc(line, max_line);
  152>             if (!new_line) {
  153                  ini_free(line);
  154                  return -2;
  155              }
```
### `{subject}/ini.c:157:0` in `ini_parse_stream` (regions: G_heap_buffer, G_reader_buffer)
```c
  154                  return -2;
  155              }
  156              line = new_line;
  157>             if (reader(line + offset, (int)(max_line - offset), stream) == NULL)
  158                  break;
  159              offset += strlen(line + offset);
  160          }
```
### `{subject}/ini.c:171:1` in `ini_parse_stream` (regions: G_heap_buffer, G_reader_buffer)
```c
  168                  if (!error)
  169                      error = lineno;
  170                  abyss_len = strlen(abyss);
  171>                 if (abyss_len > 0 && abyss[abyss_len - 1] == '\n')
  172                      break;
  173              }
  174          }
```
### `{subject}/ini.c:180:1` in `ini_parse_stream` (regions: G_bom_comment)
```c
  177  #if INI_ALLOW_BOM
  178          if (lineno == 1 && (unsigned char)start[0] == 0xEF &&
  179                             (unsigned char)start[1] == 0xBB &&
  180>                            (unsigned char)start[2] == 0xBF) {
  181              start += 3;
  182          }
  183  #endif
```
### `{subject}/ini.c:190:3` in `ini_parse_stream` (regions: G_continuation)
```c
  187              /* Start-of-line comment */
  188          }
  189  #if INI_ALLOW_MULTILINE
  190>         else if (*prev_name && *start && start > line) {
  191  #if INI_ALLOW_INLINE_COMMENTS
  192              end = ini_find_chars_or_comment(start, NULL);
  193              *end = '\0';
```
### `{subject}/ini.c:221:1` in `ini_parse_stream`
```c
  218                  error = lineno;
  219              }
  220          }
  221>         else if (*start) {
  222              /* Not a comment, must be a name[=:]value pair */
  223              end = ini_find_chars_or_comment(start, "=:");
  224              if (*end == '=' || *end == ':') {
```
### `{subject}/ini.c:302:2` in `ini_reader_string` (regions: G_reader_buffer)
```c
  299      char* strp = str;
  300      char c;
  301  
  302>     if (ctx_num_left == 0 || num < 2)
  303          return NULL;
  304  
  305      while (num > 1 && ctx_num_left != 0) {
```

## Never-executed lines (4)

### `{subject}/ini.c:131` in `ini_parse_stream` (regions: G_heap_buffer, G_custom_alloc)
```c
  128  #if !INI_USE_STACK
  129      line = (char*)ini_malloc(INI_INITIAL_ALLOC);
  130      if (!line) {
  131>         return -2;
  132      }
  133  #endif
  134  
```
### `{subject}/ini.c:153` in `ini_parse_stream` (regions: G_heap_buffer, G_custom_alloc, G_reader_buffer)
```c
  150                  max_line = INI_MAX_LINE;
  151              new_line = ini_realloc(line, max_line);
  152              if (!new_line) {
  153>                 ini_free(line);
  154                  return -2;
  155              }
  156              line = new_line;
```
### `{subject}/ini.c:154` in `ini_parse_stream` (regions: G_heap_buffer, G_custom_alloc, G_reader_buffer)
```c
  151              new_line = ini_realloc(line, max_line);
  152              if (!new_line) {
  153                  ini_free(line);
  154>                 return -2;
  155              }
  156              line = new_line;
  157              if (reader(line + offset, (int)(max_line - offset), stream) == NULL)
```
### `{subject}/ini.c:158` in `ini_parse_stream` (regions: G_heap_buffer, G_reader_buffer)
```c
  155              }
  156              line = new_line;
  157              if (reader(line + offset, (int)(max_line - offset), stream) == NULL)
  158>                 break;
  159              offset += strlen(line + offset);
  160          }
  161  #endif
```

Gcov numbers branch outcomes per line in source order (index 0 = first outcome gcov lists for that line); for `if (a && b)` the outcomes alternate true/false per condition.

## Which settings execute which code (attribution from the runs)

- `INI_USE_STACK=0` -> lines 104,129-130,266
- `INI_ALLOW_MULTILINE=1` -> lines 111,190
- `INI_ALLOW_BOM=1` -> lines 178-179
- `INI_ALLOW_NO_VALUE=1` -> lines 245-246,250
- `INI_ALLOW_NO_VALUE=0` -> lines 253
- `INI_STOP_ON_FIRST_ERROR=1` -> lines 260-261
- `INI_ALLOW_INLINE_COMMENTS=1` -> lines 68-69,71-72
- `INI_HANDLER_LINENO=0` -> lines 

## Configuration model

- `INI_HANDLER_LINENO` (compile_time, default 0): settings [0, 1]
- `INI_ALLOW_MULTILINE` (compile_time, default 1): settings [1, 0]
- `INI_ALLOW_BOM` (compile_time, default 1): settings [1, 0]
- `INI_START_COMMENT_PREFIXES` (compile_time, default ';#'): settings [';#', ';', '#', '']
- `INI_ALLOW_INLINE_COMMENTS` (compile_time, default 1): settings [1, 0]
- `INI_INLINE_COMMENT_PREFIXES` (compile_time, default ';'): settings [';', '#', ';#']; active when INI_ALLOW_INLINE_COMMENTS == 1
- `INI_USE_STACK` (compile_time, default 1): settings [1, 0]
- `INI_MAX_LINE` (compile_time, default 200): settings [2, 20, 200, 4096]; active when (INI_USE_STACK == 1 or INI_ALLOW_REALLOC == 1)
- `INI_MAX_SECTION` (compile_time, default 50): settings [1, 8, 50, 256]
- `INI_MAX_NAME` (compile_time, default 50): settings [1, 8, 50, 256]; active when INI_ALLOW_MULTILINE == 1
- `INI_ALLOW_REALLOC` (compile_time, default 0): settings [0, 1]; active when INI_USE_STACK == 0
- `INI_INITIAL_ALLOC` (compile_time, default 200): settings [2, 5, 20, 200]; active when INI_USE_STACK == 0
- `INI_STOP_ON_FIRST_ERROR` (compile_time, default 0): settings [0, 1]
- `INI_CALL_HANDLER_ON_NEW_SECTION` (compile_time, default 0): settings [0, 1]
- `INI_ALLOW_NO_VALUE` (compile_time, default 0): settings [0, 1]
- `INI_CUSTOM_ALLOCATOR` (compile_time, default 0): settings [0, 1]; active when INI_USE_STACK == 0
- `api_entry_point` (runtime, default 'ini_parse'): settings ['ini_parse', 'ini_parse_file', 'ini_parse_stream', 'ini_parse_string', 'ini_parse_string_length']
- `handler_return_policy` (runtime, default 'reject_flagged'): settings ['reject_flagged', 'accept_all', 'reject_all']

- constraint C1 (hard, confirmed): `if INI_CUSTOM_ALLOCATOR == 1 then INI_USE_STACK == 0`
- constraint C2 (soft, confirmed): `if INI_USE_STACK == 0 and INI_ALLOW_REALLOC == 1 then INI_INITIAL_ALLOC <= INI_MAX_LINE`
- constraint C3 (soft, confirmed): `if INI_USE_STACK == 0 and INI_ALLOW_REALLOC == 0 then INI_INITIAL_ALLOC == INI_MAX_LINE`
- group G_heap_buffer (t=3): ['INI_USE_STACK', 'INI_ALLOW_REALLOC', 'INI_INITIAL_ALLOC', 'INI_MAX_LINE']; region ini.c:99-108,128-133,141-173
- group G_custom_alloc (t=3): ['INI_USE_STACK', 'INI_CUSTOM_ALLOCATOR', 'INI_ALLOW_REALLOC']; region ini.c:25-37,128-133,150-155,264-267
- group G_reader_buffer (t=3): ['api_entry_point', 'INI_ALLOW_REALLOC', 'INI_MAX_LINE']; region ini.c:141-173,295-319
- group G_continuation (t=3): ['INI_ALLOW_MULTILINE', 'INI_ALLOW_INLINE_COMMENTS', 'INI_START_COMMENT_PREFIXES', 'INI_MAX_NAME']; region ini.c:110-111,186-201,235-237
- group G_no_value (t=3): ['INI_ALLOW_NO_VALUE', 'INI_ALLOW_MULTILINE', 'handler_return_policy']; region ini.c:242-256
- group G_error_flow (t=3): ['INI_STOP_ON_FIRST_ERROR', 'handler_return_policy', 'INI_CALL_HANDLER_ON_NEW_SECTION']; region ini.c:198-199,211-214,239-240,250-251,259-262
- group G_comment_prefixes (t=3): ['INI_ALLOW_INLINE_COMMENTS', 'INI_INLINE_COMMENT_PREFIXES', 'INI_START_COMMENT_PREFIXES']; region ini.c:65-78,186,191-195,228-231
- group G_bom_comment (t=2): ['INI_ALLOW_BOM', 'INI_START_COMMENT_PREFIXES']; region ini.c:177-186

## Existing builds (compile-time configurations already in the runs; reuse them for C5): 48

```json
[{"INI_ALLOW_BOM": 1, "INI_ALLOW_INLINE_COMMENTS": 1, "INI_ALLOW_MULTILINE": 1, "INI_ALLOW_NO_VALUE": 0, "INI_ALLOW_REALLOC": 0, "INI_CALL_HANDLER_ON_NEW_SECTION": 0, "INI_CUSTOM_ALLOCATOR": 0, "INI_HANDLER_LINENO": 0, "INI_INITIAL_ALLOC": 200, "INI_INLINE_COMMENT_PREFIXES": ";", "INI_MAX_LINE": 200, "INI_MAX_NAME": 50, "INI_MAX_SECTION": 50, "INI_START_COMMENT_PREFIXES": ";#", "INI_STOP_ON_FIRST_ERROR": 0, "INI_USE_STACK": 1}, {"INI_ALLOW_BOM": 1, "INI_ALLOW_INLINE_COMMENTS": 1, "INI_ALLOW_MULTILINE": 1, "INI_ALLOW_NO_VALUE": 0, "INI_ALLOW_REALLOC": 0, "INI_CALL_HANDLER_ON_NEW_SECTION": 0, "INI_CUSTOM_ALLOCATOR": 0, "INI_HANDLER_LINENO": 0, "INI_INITIAL_ALLOC": 200, "INI_INLINE_COMMENT_PREFIXES": ";", "INI_MAX_LINE": 20, "INI_MAX_NAME": 50, "INI_MAX_SECTION": 50, "INI_START_COMMENT_PREFIXES": ";#", "INI_STOP_ON_FIRST_ERROR": 0, "INI_USE_STACK": 1}, {"INI_ALLOW_BOM": 1, "INI_ALLOW_INLINE_COMMENTS": 1, "INI_ALLOW_MULTILINE": 0, "INI_ALLOW_NO_VALUE": 0, "INI_ALLOW_REALLOC": 0, "INI_CALL_HANDLER_ON_NEW_SECTION": 0, "INI_CUSTOM_ALLOCATOR": 0, "INI_HANDLER_LINENO": 0, "INI_INITIAL_ALLOC": 200, "INI_INLINE_COMMENT_PREFIXES": ";", "INI_MAX_LINE": 200, "INI_MAX_NAME": 50, "INI_MAX_SECTION": 50, "INI_START_COMMENT_PREFIXES": ";#", "INI_STOP_ON_FIRST_ERROR": 0, "INI_USE_STACK": 1}, {"INI_ALLOW_BOM": 1, "INI_ALLOW_INLINE_COMMENTS": 0, "INI_ALLOW_MULTILINE": 1, "INI_ALLOW_NO_VALUE": 0, "INI_ALLOW_REALLOC": 0, "INI_CALL_HANDLER_ON_NEW_SECTION": 0, "INI_CUSTOM_ALLOCATOR": 0, "INI_HANDLER_LINENO": 0, "INI_INITIAL_ALLOC": 200, "INI_INLINE_COMMENT_PREFIXES": ";", "INI_MAX_LINE": 200, "INI_MAX_NAME": 50, "INI_MAX_SECTION": 50, "INI_START_COMMENT_PREFIXES": ";#", "INI_STOP_ON_FIRST_ERROR": 0, "INI_USE_STACK": 1}, {"INI_ALLOW_BOM": 1, "INI_ALLOW_INLINE_COMMENTS": 1, "INI_ALLOW_MULTILINE": 1, "INI_ALLOW_NO_VALUE": 0, "INI_ALLOW_REALLOC": 0, "INI_CALL_HANDLER_ON_NEW_SECTION": 0, "INI_CUSTOM_ALLOCATOR": 0, "INI_HANDLER_LINENO": 0, "INI_INITIAL_ALLOC": 200, "INI_INLINE_COMMENT_PREFIXES": ";", "INI_MAX_LINE": 200, "INI_MAX_NAME": 50, "INI_MAX_SECTION": 50, "INI_START_COMMENT_PREFIXES": ";#", "INI_STOP_ON_FIRST_ERROR": 1, "INI_USE_STACK": 1}, {"INI_ALLOW_BOM": 1, "INI_ALLOW_INLINE_COMMENTS": 1, "INI_ALLOW_MULTILINE": 1, "INI_ALLOW_NO_VALUE": 0, "INI_ALLOW_REALLOC": 0, "INI_CALL_HANDLER_ON_NEW_SECTION": 0, "INI_CUSTOM_ALLOCATOR": 0, "INI_HANDLER_LINENO": 1, "INI_INITIAL_ALLOC": 200, "INI_INLINE_COMMENT_PREFIXES": ";", "INI_MAX_LINE": 200, "INI_MAX_NAME": 50, "INI_MAX_SECTION": 50, "INI_START_COMMENT_PREFIXES": ";#", "INI_STOP_ON_FIRST_ERROR": 0, "INI_USE_STACK": 1}, {"INI_ALLOW_BOM": 1, "INI_ALLOW_INLINE_COMMENTS": 1, "INI_ALLOW_MULTILINE": 1, "INI_ALLOW_NO_VALUE": 0, "INI_ALLOW_REALLOC": 0, "INI_CALL_HANDLER_ON_NEW_SECTION": 0, "INI_CUSTOM_ALLOCATOR": 0, "INI_HANDLER_LINENO": 0, "INI_INITIAL_ALLOC": 200, "INI_INLINE_COMMENT_PREFIXES": ";", "INI_MAX_LINE": 200, "INI_MAX_NAME": 50, "INI_MAX_SECTION": 50, "INI_START_COMMENT_PREFIXES": ";#", "INI_STOP_ON_FIRST_ERROR": 0, "INI_USE_STACK": 0}, {"INI_ALLOW_BOM": 1, "INI_ALLOW_INLINE_COMMENTS": 1, "INI_ALLOW_MULTILINE": 1, "INI_ALLOW_NO_VALUE": 0, "INI_ALLOW_REALLOC": 0, "INI_CALL_HANDLER_ON_NEW_SECTION": 0, "INI_CUSTOM_ALLOCATOR": 0, "INI_HANDLER_LINENO": 0, "INI_INITIAL_ALLOC": 20, "INI_INLINE_COMMENT_PREFIXES": ";", "INI_MAX_LINE": 200, "INI_MAX_NAME": 50, "INI_MAX_SECTION": 50, "INI_START_COMMENT_PREFIXES": ";#", "INI_STOP_ON_FIRST_ERROR": 0, "INI_USE_STACK": 0}, {"INI_ALLOW_BOM": 1, "INI_ALLOW_INLINE_COMMENTS": 1, "INI_ALLOW_MULTILINE": 1, "INI_ALLOW_NO_VALUE": 0, "INI_ALLOW_REALLOC": 1, "INI_CALL_HANDLER_ON_NEW_SECTION": 0, "INI_CUSTOM_ALLOCATOR": 0, "INI_HANDLER_LINENO": 0, "INI_INITIAL_ALLOC": 5, "INI_INLINE_COMMENT_PREFIXES": ";", "INI_MAX_LINE": 200, "INI_MAX_NAME": 50, "INI_MAX_SECTION": 50, "INI_START_COMMENT_PREFIXES": ";#", "INI_STOP_ON_FIRST_ERROR": 0, "INI_USE_STACK": 0}, {"INI_ALLOW_BOM": 1, "INI_ALLOW_INLINE_COMMENTS": 1, "INI_ALLOW_MULTILINE": 1, "INI_ALLOW_NO_VALUE": 0, "INI_ALLOW_REALLOC":
```

## Existing test inputs

- developer inputs (tests/): no_file.ini, normal.ini, bad_section.ini, bad_comment.ini, user_error.ini, multi_line.ini, bad_multi.ini, bom.ini, duplicate_sections.ini, no_value.ini, long_section.ini, long_line.ini, name_only_after_error.ini
- generated inputs: gen_long_name_multiline.ini, gen_inline_comment_mix.ini, gen_bom_hash_comment.ini, gen_novalue_continuation.ini, gen_error_then_sections.ini, gen_crlf_boundary.ini, gen_hash_key_and_colon.ini, gen_overlong_line.ini, gen_continuation_handler_error.ini

## Test contract

`csm_driver` (workspace/drivers/csm_driver.c) is the generated, runtime-configurable driver:
`csm_driver --api <api_entry_point> --handler <handler_return_policy> -- <input files>`.
For each input it prints the callback trace and a summary line (identical format to the developer
driver tests/unittest.c):

    ... [section]            <- printed when the section changes (or on a section callback)
    ... name=value;          <- one line per name/value callback (no '=value' when value is NULL)
    <file>: e=<error line or -1/-2> user=<n>

`e` is ini_parse's return value: 0 = success, >0 = first error line, -1 = open failure,
-2 = allocation failure. With INI_HANDLER_LINENO=1 every callback line ends in '  line N'.
handler_return_policy: reject_flagged = callback fails only for user=parse_error;
accept_all = never fails; reject_all = every callback fails.
The differential oracle re-parses each input through a second entry point
(ini_parse_string_length, or ini_parse when that is the selected one) and requires an identical trace
and return value. Inputs are processed in the order given; the 'user' counter starts at 100 and
increases by one per input file, so for a test's own input expect user=101 only if it is the only input:
the harness appends test inputs AFTER the 22 standard inputs, so prefer 'contains' over 'lines'
unless you account for this.


## Source of the system under test

```c
/* inih -- simple .INI file parser

SPDX-License-Identifier: BSD-3-Clause

Copyright (C) 2009-2025, Ben Hoyt

inih is released under the New BSD license (see LICENSE.txt). Go to the project
home page for more info:

https://github.com/benhoyt/inih

*/

#if defined(_MSC_VER) && !defined(_CRT_SECURE_NO_WARNINGS)
#define _CRT_SECURE_NO_WARNINGS
#endif

#include <stdio.h>
#include <ctype.h>
#include <string.h>
#include <assert.h>

#include "ini.h"

#if !INI_USE_STACK
#if INI_CUSTOM_ALLOCATOR
#include <stddef.h>
void* ini_malloc(size_t size);
void ini_free(void* ptr);
void* ini_realloc(void* ptr, size_t size);
#else
#include <stdlib.h>
#define ini_malloc malloc
#define ini_free free
#define ini_realloc realloc
#endif
#endif

/* Used by ini_parse_string() to keep track of string parsing state. */
typedef struct {
    const char* ptr;
    size_t num_left;
} ini_parse_string_ctx;

/* Strip whitespace chars off end of given string, in place. end must be a
   pointer to the NUL terminator at the end of the string. Return s. */
static char* ini_rstrip(char* s, char* end)
{
    while (end > s && isspace((unsigned char)(*--end)))
        *end = '\0';
    return s;
}

/* Return pointer to first non-whitespace char in given string. */
static char* ini_lskip(const char* s)
{
    while (*s && isspace((unsigned char)(*s)))
        s++;
    return (char*)s;
}

/* Return pointer to first char (of chars) or inline comment in given string,
   or pointer to NUL at end of string if neither found. Inline comment must
   be prefixed by a whitespace character to register as a comment. */
static char* ini_find_chars_or_comment(const char* s, const char* chars)
{
#if INI_ALLOW_INLINE_COMMENTS
    int was_space = 0;
    while (*s && (!chars || !strchr(chars, *s)) &&
           !(was_space && strchr(INI_INLINE_COMMENT_PREFIXES, *s))) {
        was_space = isspace((unsigned char)(*s));
        s++;
    }
#else
    while (*s && (!chars || !strchr(chars, *s))) {
        s++;
    }
#endif
    return (char*)s;
}

/* Similar to strncpy, but ensures dest (size bytes) is
   NUL-terminated, and doesn't pad with NULs. */
static char* ini_strncpy0(char* dest, const char* src, size_t size)
{
    /* Could use strncpy internally, but it causes gcc warnings (see issue #91) */
    size_t i;
    for (i = 0; i < size - 1 && src[i]; i++)
        dest[i] = src[i];
    dest[i] = '\0';
    return dest;
}

/* See documentation in header file. */
int ini_parse_stream(ini_reader reader, void* stream, ini_handler handler,
                     void* user)
{
    /* Uses a fair bit of stack (use heap instead if you need to) */
#if INI_USE_STACK
    char line[INI_MAX_LINE];
    size_t max_line = INI_MAX_LINE;
#else
    char* line;
    size_t max_line = INI_INITIAL_ALLOC;
#endif
#if INI_ALLOW_REALLOC && !INI_USE_STACK
    char* new_line;
#endif
    char section[INI_MAX_SECTION] = "";
#if INI_ALLOW_MULTILINE
    char prev_name[INI_MAX_NAME] = "";
#endif

    size_t offset;
    char* start;
    char* end;
    char* name;
    char* value;
    int lineno = 0;
    int error = 0;
    char abyss[16];  /* Used to consume input when a line is too long. */
    size_t abyss_len;

    assert(reader != NULL);
    assert(stream != NULL);
    assert(handler != NULL);

#if !INI_USE_STACK
    line = (char*)ini_malloc(INI_INITIAL_ALLOC);
    if (!line) {
        return -2;
    }
#endif

#if INI_HANDLER_LINENO
#define HANDLER(u, s, n, v) handler(u, s, n, v, lineno)
#else
#define HANDLER(u, s, n, v) handler(u, s, n, v)
#endif

    /* Scan through stream line by line */
    while (reader(line, (int)max_line, stream) != NULL) {
        offset = strlen(line);

#if INI_ALLOW_REALLOC && !INI_USE_STACK
        while (max_line < INI_MAX_LINE &&
               offset == max_line - 1 && line[offset - 1] != '\n') {
            max_line *= 2;
            if (max_line > INI_MAX_LINE)
                max_line = INI_MAX_LINE;
            new_line = ini_realloc(line, max_line);
            if (!new_line) {
                ini_free(line);
                return -2;
            }
            line = new_line;
            if (reader(line + offset, (int)(max_line - offset), stream) == NULL)
                break;
            offset += strlen(line + offset);
        }
#endif

        lineno++;

        /* If line exceeded INI_MAX_LINE bytes, discard till end of line. */
        if (offset == max_line - 1 && line[offset - 1] != '\n') {
            while (reader(abyss, sizeof(abyss), stream) != NULL) {
                if (!error)
                    error = lineno;
                abyss_len = strlen(abyss);
                if (abyss_len > 0 && abyss[abyss_len - 1] == '\n')
                    break;
            }
        }

        start = line;
#if INI_ALLOW_BOM
        if (lineno == 1 && (unsigned char)start[0] == 0xEF &&
                           (unsigned char)start[1] == 0xBB &&
                           (unsigned char)start[2] == 0xBF) {
            start += 3;
        }
#endif
        start = ini_rstrip(ini_lskip(start), line + offset);

        if (strchr(INI_START_COMMENT_PREFIXES, *start)) {
            /* Start-of-line comment */
        }
#if INI_ALLOW_MULTILINE
        else if (*prev_name && *start && start > line) {
#if INI_ALLOW_INLINE_COMMENTS
            end = ini_find_chars_or_comment(start, NULL);
            *end = '\0';
            ini_rstrip(start, end);
#endif
            /* Non-blank line with leading whitespace, treat as continuation
               of previous name's value (as per Python configparser). */
            if (!HANDLER(user, section, prev_name, start) && !error)
                error = lineno;
        }
#endif
        else if (*start == '[') {
            /* A "[section]" line */
            end = ini_find_chars_or_comment(start + 1, "]");
            if (*end == ']') {
                *end = '\0';
                ini_strncpy0(section, start + 1, sizeof(section));
#if INI_ALLOW_MULTILINE
                *prev_name = '\0';
#endif
#if INI_CALL_HANDLER_ON_NEW_SECTION
                if (!HANDLER(user, section, NULL, NULL) && !error)
                    error = lineno;
#endif
            }
            else if (!error) {
                /* No ']' found on section line */
                error = lineno;
            }
        }
        else if (*start) {
            /* Not a comment, must be a name[=:]value pair */
            end = ini_find_chars_or_comment(start, "=:");
            if (*end == '=' || *end == ':') {
                *end = '\0';
                name = ini_rstrip(start, end);
                value = end + 1;
#if INI_ALLOW_INLINE_COMMENTS
                end = ini_find_chars_or_comment(value, NULL);
                *end = '\0';
#endif
                value = ini_lskip(value);
                ini_rstrip(value, end);

#if INI_ALLOW_MULTILINE
                ini_strncpy0(prev_name, name, sizeof(prev_name));
#endif
                /* Valid name[=:]value pair found, call handler */
                if (!HANDLER(user, section, name, value) && !error)
                    error = lineno;
            }
            else {
                /* No '=' or ':' found on name[=:]value line */
#if INI_ALLOW_NO_VALUE
                *end = '\0';
                name = ini_rstrip(start, end);
#if INI_ALLOW_MULTILINE
                ini_strncpy0(prev_name, name, sizeof(prev_name));
#endif
                if (!HANDLER(user, section, name, NULL) && !error)
                    error = lineno;
#else
                if (!error)
                    error = lineno;
#endif
            }
        }

#if INI_STOP_ON_FIRST_ERROR
        if (error)
            break;
#endif
    }

#if !INI_USE_STACK
    ini_free(line);
#endif

    return error;
}

/* See documentation in header file. */
int ini_parse_file(FILE* file, ini_handler handler, void* user)
{
    int error = ini_parse_stream((ini_reader)fgets, file, handler, user);
    return ferror(file) ? -3 : error;
}

/* See documentation in header file. */
int ini_parse(const char* filename, ini_handler handler, void* user)
{
    FILE* file;
    int error;

    file = fopen(filename, "r");
    if (!file)
        return -1;
    error = ini_parse_file(file, handler, user);
    fclose(file);
    return error;
}

/* An ini_reader function to read the next line from a string buffer. This
   is the fgets() equivalent used by ini_parse_string(). */
static char* ini_reader_string(char* str, int num, void* stream) {
    ini_parse_string_ctx* ctx = (ini_parse_string_ctx*)stream;
    const char* ctx_ptr = ctx->ptr;
    size_t ctx_num_left = ctx->num_left;
    char* strp = str;
    char c;

    if (ctx_num_left == 0 || num < 2)
        return NULL;

    while (num > 1 && ctx_num_left != 0) {
        c = *ctx_ptr++;
        ctx_num_left--;
        *strp++ = c;
        if (c == '\n')
            break;
        num--;
    }

    *strp = '\0';
    ctx->ptr = ctx_ptr;
    ctx->num_left = ctx_num_left;
    return str;
}

/* See documentation in header file. */
int ini_parse_string(const char* string, ini_handler handler, void* user) {
    return ini_parse_string_length(string, strlen(string), handler, user);
}

/* See documentation in header file. */
int ini_parse_string_length(const char* string, size_t length,
                            ini_handler handler, void* user) {
    ini_parse_string_ctx ctx;

    ctx.ptr = string;
    ctx.num_left = length;
    return ini_parse_stream((ini_reader)ini_reader_string, &ctx, handler,
                            user);
}

```

## Answer format

Answer with ONE fenced ```yaml block containing:

analysis:                     # your gap analysis, most important first
  - target: "ini.c:158"       # a line "file:line", a branch outcome "file:line:index", or a group tuple
                              #   e.g. "G_reader_buffer{api_entry_point=ini_parse_string,INI_ALLOW_REALLOC=1,INI_MAX_LINE=2}"
    criterion: C1 | C2 | C3 | C4
    reachable: input_and_configuration | needs_fault_injection | unreachable
    reasoning: <why the gap exists and what reaches it>

constraint_hypotheses:        # invalid / contradictory combinations or hidden constraints you found
  - id: <short id>
    kind: hard | soft
    expr: <CSM expression, e.g. if INI_USE_STACK == 0 and INI_ALLOW_REALLOC == 1 then INI_INITIAL_ALLOC <= INI_MAX_LINE>
    rationale: <why>
    evidence: [{file: ini.c, lines: "145-161", kind: use}]

support_drivers:              # optional: fault-injection variants of an existing generated driver
  - id: <identifier>          # e.g. csm_driver_failalloc
    base: csm_driver
    source_name: <file>.c     # linked INSTEAD of the default allocator shim; must define ini_malloc,
    content: |                #   ini_free and ini_realloc (used with INI_CUSTOM_ALLOCATOR=1, INI_USE_STACK=0)
      ...C code...
    compatible_when: "INI_USE_STACK == 0 and INI_CUSTOM_ALLOCATOR == 1"

tests:
  - id: <identifier>                       # unique, [A-Za-z_][A-Za-z0-9_]*
    targets: ["ini.c:158", "ini.c:302:2"]  # what this test must execute (checked with per-test coverage)
    criterion: C1 | C2 | C3 | C4
    configuration: {INI_USE_STACK: 0, INI_ALLOW_REALLOC: 1, api_entry_point: ini_parse_string}  # others default
    driver: csm_driver                     # or a support driver id
    env: {NAME: value}                     # optional environment for the driver process
    input:
      name: <file>.ini                     # unique file name; it is also the test-case name
      content: "[s]\nkey = value\n"        # YAML double-quoted string; use \r, \t, \xNN escapes as needed
    expected:                              # oracle: one of
      differential: true                   #   the selected entry point must agree with the reference one
      contains: ["... key=value;"]          #   substrings of the callback trace (see the driver contract)
      lines: ["... [s]", "... key=value;", "<file>.ini: e=0 user=101"]   # exact trace
