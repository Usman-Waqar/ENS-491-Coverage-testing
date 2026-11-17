# Iteration 01: failure-revealing tests, preliminary triage

The triage was done in the same agent session (claude-opus-5-5) that generated the tests. Per the
proposal (§3.3, fourth criterion), each finding needs **confirmation by a team member** before it
is reported to the maintainers. Subject: inih @ `611ab51`.

## F1: an overlong line with a NUL byte in its tail swallows the next line (robustness defect)

**Test:** `t_abyss_nul` (`INI_MAX_LINE=20`). It was reproduced with the **default** configuration
by `scratchpad repro.c`: a 199-character line, then `\0yyy\n`, then `after=ok\n`, then `last=1\n`.

**Observed:** `ini_parse` returns 2 (correct: line 2 is too long), but `after=ok` never reaches the
handler. `last=1` does. There is no sanitizer report.

**Root cause:** the discard loop (`ini.c` 165-173) stops when the chunk read into `abyss` ends in
`'\n'`, using `strlen(abyss)`. If the chunk starts with a NUL byte, `strlen` is 0, the
newline in that chunk goes unseen, and the loop reads and discards the following line too.

**Configuration dependence:** any build in which the line buffer can overflow (every
configuration; the line length is relative to `INI_MAX_LINE` / `INI_INITIAL_ALLOC`). All five entry
points behave the same, because the differential oracle passes.

**Severity:** low. It needs a NUL byte in a text file. But it silently drops a valid line, and
the developer suite does not exercise it.

## F2: continuation lines of long names are reported under a truncated name (interaction `INI_ALLOW_MULTILINE` × `INI_MAX_NAME`)

**Test:** `t_maxname_trunc_continuation` (`INI_MAX_NAME=8`).

**Observed:** `longname_x=first`, then the continuation line as `longnam=second`.

**Root cause:** names in `name=value` callbacks are passed through untruncated, but the copy kept
for continuation lines (`prev_name`, `ini.c` 236) is truncated to `INI_MAX_NAME-1`.

**Documentation:** `ini.h` 152-156 says names longer than `INI_MAX_NAME` "are truncated as above",
which is not what happens to the first callback. A client that stores values by name receives the
continuation under a key it has never seen.

**Default build:** also occurs with names of 50 or more characters (`tests/../gen_long_name_multiline.ini`
already exercised this path, but only under the differential oracle, so nobody noticed).

**Severity:** low to medium; data is attributed to the wrong key. This is the kind of
interaction fault the project targets: it needs both options and a specific input.

## F3: `INI_MAX_NAME=1` silently disables multi-line continuation (configuration pitfall)

**Test:** `t_maxname1_multiline`.

**Observed:** the indented line is parsed as a name without a value: error, `e=3`.

**Root cause:** `prev_name` is always empty, so the `*prev_name` test at `ini.c` 190 never holds.

**Classification:** not a code bug but a contradictory configuration: multi-line support is on
and cannot work. It was recorded in `csm.yaml` as soft constraint **C4**
(`if INI_ALLOW_MULTILINE == 1 then INI_MAX_NAME >= 2`, status confirmed, origin llm, confirmed
by this test).

## Gaps classified as unreachable (no test written)

| Gap | Reason |
|---|---|
| `ini.c:75:2` | `!chars` true in the `INI_ALLOW_INLINE_COMMENTS=0` build. Every NULL-`chars` call is inside `#if INI_ALLOW_INLINE_COMMENTS`. |
| `ini.c:124:0`, `125:0`, `126:0` | `assert` false branches; API misuse only. |
| `ini.c:190:3`, `221:1` | `*start == 0` never gets past the start-of-line comment test: `strchr(prefixes, '\0')` always matches. |
| `ini.c:302:2` | `num < 2` needs `INI_MAX_LINE=1` or `INI_INITIAL_ALLOC=1`, both below the valid range. |

These are themselves findings for RQ2: the remaining "coverage gaps" are dead code under every
valid configuration. The two at 190 and 221 suggest a redundant condition in the parser.
