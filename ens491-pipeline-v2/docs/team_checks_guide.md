# Team checks: reference model, LLM discovery, bug confirmation

These are the three checks only a human team can do. All commands assume WSL/Ubuntu, the
unpacked tarball in your home folder (`~/workspace` next to `~/subject_system`), and that you
start in `~/workspace`.

Order matters: **Check 1 first**, because Checks 2's scores are measured against the reviewed model.
Check 3 is independent and can be done any time.

Before starting, keep an untouched copy of the model so you can show what the review changed:

```bash
cd ~/workspace
cp csm.yaml csm_before_review.yaml
```

---

## Check 1: review the reference model (`csm.yaml`), about 2–3 hours

**Why:** RQ1 measures how well an LLM recovers the configuration model. The yardstick is
`csm.yaml`. I (an LLM) wrote it, so until people have checked it, it is not ground truth.

**How to split the work (recommended):** you both review *all* of it **independently**, then
compare notes. Where you disagree, decide together and write the decision down. The number of
disagreements is itself a result you can report (inter-rater agreement). If time is short,
split the options in half instead.

### What to open

Open `csm.yaml` in an editor (VS Code: `code csm.yaml` from WSL), and next to it:
`../subject_system/ini.h`, `../subject_system/ini.c` and `../subject_system/README.md`.

A one-line-per-option overview helps:

```bash
python3 csm_tool.py summary csm.yaml
```

To find every place an option appears:

```bash
grep -n "INI_MAX_LINE" ../subject_system/ini.h ../subject_system/ini.c ../subject_system/README.md
```

### Checklist for each of the 18 options

For each entry under `options:`:

1. **Name and existence.** The option really exists: for compile-time options, an `#ifndef NAME` /
   `#define NAME value` block in `ini.h`.
2. **Default.** `default:` equals the value in that `#define`. For the two runtime options
   (`api_entry_point`, `handler_return_policy`), check that the default is a sensible convention
   (`default_source: docs / convention`).
3. **Type.** `boolean` (0/1), `integer`, `string` or `enum` matches how the code uses it.
4. **Settings.**
   - Booleans: nothing to check.
   - Strings: are the listed values (`";#"`, `";"`, `"#"`, `""` …) the meaningful cases?
   - Integers: look at `partitions`. Each is an interval with one representative value. Ask:
     do the intervals cover the whole `range`, and is each interval a real "behaves the same"
     class? Example: `INI_MAX_LINE` small = 3–199 (some test lines overflow), default = 200,
     large = 201+ (no test line overflows). You may disagree and change the boundaries; write
     why in `rationale`.
5. **`active_when`** (only six options have it). Is the option really ignored when the
   condition is false? Example: `INI_INITIAL_ALLOC` has `active_when: INI_USE_STACK == 0`,
   because it only appears inside `#if !INI_USE_STACK` blocks in `ini.c`. Check by reading the
   `#if` blocks around each use.
6. **Evidence lines.** Spot-check two or three `evidence` entries per option: open the file at
   that line and confirm it is about this option.

### Checklist for constraints (C1–C5) and groups

- **Constraints** (`constraints:` section). For each one, read `expr` and `rationale` and decide:
  - is it true?
  - is `kind` right (hard = invalid configuration; soft = legal but contradictory)?
  - is anything missing? Look especially at the comments in `ini.h` lines 104–200.
  - C5 has `status: hypothesis`. Either confirm it (change to `confirmed` and say why) or
    leave it.
- **Interaction groups** (`interaction_groups:`). These are "options that control the same code".
  For each group, open the `region` lines in `ini.c` and decide whether those options really
  interact there.

### Recording what you did

- **Fix** anything wrong directly in `csm.yaml`.
- **Mark** each option, constraint and group you checked, by adding `human` to `discovered_by`
  in its provenance and a note. For example:
  ```yaml
  provenance: {discovered_by: [llm, human], ..., status: confirmed,
               notes: "reviewed by T. Karimov and M. U. W. Khan, 2026-11-20; partitions unchanged"}
  ```
- **Update** `reference_status` under `system: extraction:` to say the model has been reviewed,
  by whom and when.
- **Write** a short review log (`docs/reference_review.md`): what you changed and why, and where
  you two disagreed.

### Validate and regenerate after editing

```bash
python3 csm_tool.py check csm.yaml          # must print "problems": []
python3 csm_tool.py yaml2json csm.yaml      # regenerates csm.json, which the other scripts read
diff csm_before_review.yaml csm.yaml        # what the review changed
./run_pipeline.sh                           # rerun everything with the reviewed model
```

If `check` reports a problem, the message names the option and the issue, e.g. a gap between
two partitions.

Optional mechanical double-check of the `active_when` conditions and integer lower bounds:

```bash
python3 discover/verify.py --root ../subject_system --csm csm.yaml --harness csm.yaml -o verify_reference.json
```

---

## Check 2: run the LLM discovery step in fresh sessions (RQ1 with an LLM), about 1 hour

**Why:** the static scanner found every option but none of the constraints. RQ1 asks how much
an LLM adds. That must be measured on an LLM that has **not** seen the answer. This chat has
(I wrote the reference model), and so has the ENS491 project (it contains the model and results).

### Avoid contamination (important)

- **Claude:** start a new chat **outside the ENS491 project**. If you have memory enabled,
  use an incognito chat or turn memory off for this.
- **ChatGPT:** use a *Temporary Chat*.
- **Gemini:** use a new chat with app activity / memory off.
- Do not paste anything except the prompt files.

### The prompt files

The run produced **two** parts (the source excerpts did not fit in one):

```
~/workspace/pipeline_out/discovery/prompts/prompt_01.md
~/workspace/pipeline_out/discovery/prompts/prompt_02.md
```

To open the folder in Windows Explorer (so you can drag the files into the chat), run this in Ubuntu:

```bash
cd ~/workspace/pipeline_out/discovery/prompts && explorer.exe .
```

Each part repeats the task and the answer format; the source excerpts differ.

### Steps per model

1. **Upload the prompt.** In the fresh chat, attach **both** files, or paste them one after the
   other, with this message:
   > These two files are parts 1 and 2 of one task. Read both, then answer once, following the
   > output contract exactly.
2. **Save the answer.** The model should answer with one block that starts with ```` ```yaml ````.
   Save the answer as a file, e.g. `~/workspace/s4_answers/claude_run1.yaml`. Pasting the whole
   answer is fine, because the script extracts the YAML block. If the model wrote no fence, save
   only the YAML part.
3. **Record what you ran.** Note the model name and version shown in the app, the date, and
   whether it was the first try.
4. **Score it:**
   ```bash
   mkdir -p s4_answers s4_runs
   python3 discover/run_discovery.py --root ../subject_system --sources ini.h ini.c --headers ini.h \
       --harness csm.yaml --reference csm.yaml --aliases discover/reference_aliases.yaml \
       --out s4_runs/claude_run1 \
       --llm-response s4_answers/claude_run1.yaml --model "Claude (fresh chat, 2026-11-20)"
   ```
   It prints two lines:
   - `S4 ingest: {...}` reports how many proposed items were accepted and how many were rejected,
     and by which guard. Rejections by G2 (invented option name) and G3 (cited lines that do not
     exist or do not mention the option) are hallucinations: count them.
   - `[llm] options P/R … constraints P/R … pairs P/R …` is the RQ1 score with the LLM's help.
     Compare it with the `[static]` line above it.
5. **Read the details:**
   - `s4_runs/claude_run1/compare_llm.md`: the score tables;
   - `s4_runs/claude_run1/s4_llm.json`: every rejected item and the reason;
   - `s4_runs/claude_run1/csm_discovered.yaml`: the model the LLM helped produce.

### Two things that may need a human decision

- **Different names for runtime options.** If the model names a runtime option differently
  (e.g. `parse_function` for the API choice), it shows up as "unmatched". If you agree it is the
  same thing as a reference option, add a line to `discover/reference_aliases.yaml`
  (`parse_function: api_entry_point`) and rerun step 4. Write down each alias you add; it is a
  judgement call.
- **Equivalent constraints written differently.** You don't need to do anything: the scorer
  compares constraints by meaning (same valid configurations), not by text.

### Make it a fair experiment

LLM answers vary. Run each model **3 times** (fresh chat each time) and report the mean and
range, e.g. constraint recall 0.67 / 1.0 / 0.67. Keep all answer files in `s4_answers/`; they
are your raw data.

---

## Check 3: confirm the three bugs, about 30 minutes

**Why:** the proposal says every failure must be confirmed by a person before you call it a
defect. My triage is in `~/workspace/phase4/iterations/01/findings.md`. Confirm the bugs with
your own small programs (below), not with my pipeline, so the check is independent.

### Set up

```bash
mkdir -p ~/workspace/repro && cd ~/workspace/repro
```

### Bug F1: a NUL byte in an overlong line swallows the next line

Create `f1.c` (`nano f1.c`, paste, save with Ctrl+O, Enter, Ctrl+X):

```c
/* F1: an overlong line whose discarded tail starts with a NUL byte swallows the next line. */
#include <stdio.h>
#include <string.h>
#include "ini.h"

static int h(void* u, const char* s, const char* n, const char* v)
{
    (void)u;
    printf("  [%s] %s=%.20s%s\n", s, n, v, (v && strlen(v) > 20) ? "..." : "");
    return 1;
}

static void write_input(int with_nul)
{
    FILE* f = fopen("f1.ini", "wb");
    fputs("[s]\nk=", f);
    for (int i = 0; i < 197; i++) fputc('x', f);   /* line 2 has 199 characters: too long for INI_MAX_LINE=200 */
    if (with_nul) fputc('\0', f);
    fputs("yyy\nafter=ok\nlast=1\n", f);
    fclose(f);
}

int main(void)
{
    write_input(0);
    printf("control (no NUL byte):\n");
    printf("  ini_parse returned %d\n", ini_parse("f1.ini", h, NULL));
    write_input(1);
    printf("with a NUL byte after the 199th character:\n");
    printf("  ini_parse returned %d\n", ini_parse("f1.ini", h, NULL));
    return 0;
}
```

Build with the **default** settings, plus memory checkers, and run:

```bash
gcc -Wall -fsanitize=address,undefined -I../../subject_system ../../subject_system/ini.c f1.c -o f1 && ./f1
```

Expected output (this is what confirms the bug):

```
control (no NUL byte):
  [s] k=xxxxxxxxxxxxxxxxxxxx...
  [s] after=ok
  [s] last=1
  ini_parse returned 2
with a NUL byte after the 199th character:
  [s] k=xxxxxxxxxxxxxxxxxxxx...
  [s] last=1
  ini_parse returned 2
```

In the second run, `after=ok` is missing: a valid line was silently dropped. Return value 2 is
correct in both runs, because line 2 is too long.

**Root cause:** read `ini.c` lines 163–173. The loop that throws away the rest of an overlong
line stops when the piece it read ends in `\n`, measured with `strlen`. A piece starting with a
NUL byte has `strlen` 0, so the newline in it is never seen, and the loop eats the next line too.

Decide together whether you agree with that explanation.

### Bugs F2 and F3: continuation lines and `INI_MAX_NAME`

Create `f2.c`:

```c
/* F2 and F3: continuation lines and INI_MAX_NAME. Compile with different -DINI_MAX_NAME values. */
#include <stdio.h>
#include "ini.h"

static int h(void* u, const char* s, const char* n, const char* v)
{
    (void)u;
    printf("  [%s] %s=%s\n", s, n, v ? v : "(null)");
    return 1;
}

int main(void)
{
    const char* text = "[s]\nlongname_x = first\n  second\n";
    printf("INI_MAX_NAME=%d\n", INI_MAX_NAME);
    printf("  ini_parse_string returned %d\n", ini_parse_string(text, h, NULL));
    return 0;
}
```

The input has a key `longname_x` whose value continues on an indented second line. Build it
three times with different name-buffer sizes:

```bash
for n in 50 8 1; do gcc -Wall -DINI_MAX_NAME=$n -I../../subject_system ../../subject_system/ini.c f2.c -o f2_$n && ./f2_$n; done
```

Expected:

```
INI_MAX_NAME=50
  [s] longname_x=first
  [s] longname_x=second
  ini_parse_string returned 0
INI_MAX_NAME=8
  [s] longname_x=first
  [s] longnam=second
  ini_parse_string returned 0
INI_MAX_NAME=1
  [s] longname_x=first
  ini_parse_string returned 3
```

- **50 (default) is the control:** both lines are reported under the same key.
- **8 is F2:** the continuation arrives under a *different, truncated* key `longnam`, which the
  program never saw before. Read `ini.h` lines 152–156: the documentation says long names are
  truncated, but the first line's name is not; only the copy kept for continuation lines is
  (`ini.c` line 236). It needs two options together (multi-line on and a small `INI_MAX_NAME`)
  plus a long key: an interaction bug. With the default 50 it happens for keys of 50 or more
  characters.
- **1 is F3:** continuation lines stop working altogether. The indented line becomes a syntax
  error, return value 3. That is a legal setting silently breaking a feature, recorded as soft
  constraint C4. It is more of a configuration pitfall than a code bug.

### Decide and record

For each finding, write in `findings.md` (or a new `docs/bug_confirmation.md`):
- reproduced: yes or no;
- do you agree with the explanation;
- your classification: bug, documentation issue, or configuration pitfall;
- severity.

### Reporting upstream (only after your supervisor agrees)

1. Check inih's existing issues first: https://github.com/benhoyt/inih/issues. Search for "NUL",
   "MAX_NAME" and "multiline".
2. If none matches, open one issue per finding with:
   - the commit (`611ab51`);
   - the build flags;
   - the small program and input;
   - expected vs actual output;
   - the root-cause line numbers.

   F1 and F2 are worth reporting. For F3 a documentation note is enough.
3. These are not security issues, so public issues are fine. Still mention them to Prof.
   Yilmaz first, since the proposal commits you to responsible reporting.

---

## Note on the ACTS Basic version

The Basic version is a free download, but it **does not support constraints or mixed strength**
(the `[Constraint]` and `[Relation]` sections). Both are needed for this model, because otherwise
ACTS would generate invalid combinations such as a custom allocator with the stack buffer. Use
Basic only to check that the tool runs and the file format is accepted. The real experiment needs
the Advanced version, which is still free but requested from NIST by e-mail.
