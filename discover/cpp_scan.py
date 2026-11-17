#!/usr/bin/env python3
"""S1 - static discovery of compile-time options from C/C++ preprocessor structure.

What it finds
  * configuration macros: `#ifndef X` immediately followed by `#define X <literal>`
    -> option X, its default, and a domain type inferred from the literal and usage
  * use sites of every option (code lines and #if/#elif expressions)
  * presence conditions: the condition under which an option can influence the
    preprocessed program. For a code use it is the conjunction of the enclosing
    #if branches; for a use inside an #if/#elif expression it is the enclosing
    condition AND the Boolean difference d(expr)/d(X) (X matters only where
    flipping it changes the branch taken). The union over all uses, minimised
    to a DNF, is the `active_when` candidate.
  * interaction-group candidates: options in the condition of an #if region
    (including enclosing regions) together with options used inside it
  * #error directives under a condition -> hard-constraint candidates
  * comments that mention two or more options -> constraint leads for the LLM stage

Host model: identifiers that are not options are evaluated as the build host's
compiler would see them (HOST_PROFILE; any other identifier is undefined = 0).

Output: JSON (stdout or -o) with 'options', 'excluded', 'groups',
'constraint_candidates', 'leads'.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
from pathlib import Path

HOST_PROFILE = {"__GNUC__": 13, "__GNUC_MINOR__": 3, "__linux__": 1, "__STDC__": 1,
                "__STDC_VERSION__": 201710, "__x86_64__": 1}

DIRECTIVE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif|define|undef|error|include|pragma|line)\b(.*)$")
IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


# --------------------------------------------------------------------------
# Comment stripping (keeps line structure)
# --------------------------------------------------------------------------

def strip_comments(text: str) -> tuple[list[str], dict[int, str]]:
    """Return (code lines with comments blanked, {line_no: comment text})."""
    out, comments = [], {}
    i, n, line = 0, len(text), 1
    buf = []
    state = "code"
    cbuf = []
    cline = 1
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "code":
            if c == "/" and nxt == "*":
                state, cbuf, cline = "block", [], line
                buf.append("  ")
                i += 2
                continue
            if c == "/" and nxt == "/":
                state, cbuf, cline = "line", [], line
                buf.append("  ")
                i += 2
                continue
            if c in "\"'":
                q = c
                buf.append(c)
                i += 1
                while i < n and text[i] != q and text[i] != "\n":
                    if text[i] == "\\" and i + 1 < n:
                        buf.append(text[i:i + 2])
                        i += 2
                        continue
                    buf.append(text[i])
                    i += 1
                if i < n and text[i] == q:
                    buf.append(q)
                    i += 1
                continue
            if c == "\n":
                out.append("".join(buf))
                buf = []
                line += 1
                i += 1
                continue
            buf.append(c)
            i += 1
        elif state == "block":
            if c == "*" and nxt == "/":
                comments[cline] = comments.get(cline, "") + "".join(cbuf)
                state = "code"
                buf.append("  ")
                i += 2
                continue
            if c == "\n":
                out.append("".join(buf))
                buf = []
                cbuf.append("\n")
                line += 1
            else:
                cbuf.append(c)
            i += 1
        else:  # line comment
            if c == "\n":
                comments[cline] = comments.get(cline, "") + "".join(cbuf)
                state = "code"
                continue
            cbuf.append(c)
            i += 1
    if buf:
        out.append("".join(buf))
    return out, comments


# --------------------------------------------------------------------------
# #if expression parser / evaluator
# --------------------------------------------------------------------------

_CTOK = re.compile(r"\s*(?:(?P<num>0[xX][0-9a-fA-F]+|\d+)[uUlL]*|(?P<id>[A-Za-z_][A-Za-z0-9_]*)|"
                   r"(?P<op>&&|\|\||==|!=|<=|>=|<<|>>|[!<>+\-*/%()?:~&|^]))")


def _ctokens(s: str) -> list:
    out, pos = [], 0
    s = s.strip()
    while pos < len(s):
        m = _CTOK.match(s, pos)
        if not m or m.end() == pos:
            raise ValueError(f"bad #if expression near {s[pos:]!r}")
        pos = m.end()
        if m.group("num"):
            out.append(("num", int(m.group("num"), 0)))
        elif m.group("id"):
            out.append(("id", m.group("id")))
        else:
            out.append(("op", m.group("op")))
    return out


class CExpr:
    """Precedence-climbing parser for preprocessor expressions."""

    BIN = {"||": 1, "&&": 2, "|": 3, "^": 4, "&": 5, "==": 6, "!=": 6, "<": 7, ">": 7, "<=": 7, ">=": 7,
           "<<": 8, ">>": 8, "+": 9, "-": 9, "*": 10, "/": 10, "%": 10}

    def __init__(self, text: str):
        self.text = text
        self.t = _ctokens(text)
        self.i = 0
        self.ast = self.expr(0)
        if self.i != len(self.t):
            raise ValueError(f"trailing tokens in #if {text!r}")

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def primary(self):
        k, v = self.take()
        if k == "num":
            return ("num", v)
        if k == "id":
            if v == "defined":
                k2, v2 = self.take()
                if (k2, v2) == ("op", "("):
                    _, name = self.take()
                    self.take()  # ')'
                    return ("defined", name)
                return ("defined", v2)
            return ("id", v)
        if (k, v) == ("op", "("):
            e = self.expr(0)
            self.take()
            return e
        if k == "op" and v in ("!", "-", "~", "+"):
            return ("un", v, self.primary())
        raise ValueError(f"unexpected token {v!r} in #if {self.text!r}")

    def expr(self, minp):
        lhs = self.primary()
        while True:
            k, v = self.peek()
            if k == "op" and v == "?" and minp == 0:
                self.take()
                a = self.expr(0)
                self.take()  # ':'
                b = self.expr(0)
                lhs = ("tern", lhs, a, b)
                continue
            if k != "op" or v not in self.BIN or self.BIN[v] < minp or minp == -1:
                return lhs
            p = self.BIN[v]
            self.take()
            rhs = self.expr(p + 1)
            lhs = ("bin", v, lhs, rhs)

    def ids(self, node=None, acc=None) -> set:
        node = self.ast if node is None else node
        acc = set() if acc is None else acc
        if node[0] in ("id", "defined"):
            acc.add(node[1])
        for ch in node[1:]:
            if isinstance(ch, tuple):
                self.ids(ch, acc)
        return acc


def c_eval(node, env: dict, defined: set) -> int:
    k = node[0]
    if k == "num":
        return node[1]
    if k == "id":
        return int(env.get(node[1], 0)) if not isinstance(env.get(node[1], 0), str) else 1
    if k == "defined":
        return 1 if node[1] in defined else 0
    if k == "un":
        v = c_eval(node[2], env, defined)
        return {"!": int(not v), "-": -v, "~": ~v, "+": v}[node[1]]
    if k == "tern":
        return c_eval(node[2] if c_eval(node[1], env, defined) else node[3], env, defined)
    op, a = node[1], c_eval(node[2], env, defined)
    if op == "&&":
        return int(bool(a) and bool(c_eval(node[3], env, defined)))
    if op == "||":
        return int(bool(a) or bool(c_eval(node[3], env, defined)))
    b = c_eval(node[3], env, defined)
    ops = {"|": a | b, "^": a ^ b, "&": a & b, "==": int(a == b), "!=": int(a != b), "<": int(a < b),
           ">": int(a > b), "<=": int(a <= b), ">=": int(a >= b), "+": a + b, "-": a - b, "*": a * b}
    if op in ops:
        return ops[op]
    if op in ("/", "%"):
        return 0 if b == 0 else (a // b if op == "/" else a % b)
    if op == "<<":
        return a << b
    if op == ">>":
        return a >> b
    raise ValueError(op)


# --------------------------------------------------------------------------
# Boolean minimisation (Quine-McCluskey, exact cover for small inputs)
# --------------------------------------------------------------------------

def minimise(vars_: list[str], truth: dict[tuple, bool]) -> list[dict]:
    """Minimal DNF of a Boolean function given as {assignment tuple: value}.
    Returns a list of terms; each term is {var: 0|1} (missing var = don't care)."""
    n = len(vars_)
    ones = [a for a, v in truth.items() if v]
    if not ones:
        return []
    if len(ones) == 2 ** n:
        return [{}]
    terms = {tuple(a) for a in ones}  # entries 0/1/None
    primes = set()
    current = terms
    while current:
        nxt, used = set(), set()
        cur = list(current)
        for i in range(len(cur)):
            for j in range(i + 1, len(cur)):
                a, b = cur[i], cur[j]
                diff = [k for k in range(n) if a[k] != b[k]]
                if len(diff) == 1 and a[diff[0]] is not None and b[diff[0]] is not None:
                    m = list(a)
                    m[diff[0]] = None
                    nxt.add(tuple(m))
                    used.add(a)
                    used.add(b)
        primes |= current - used
        current = nxt

    def covers(p, a):
        return all(p[k] is None or p[k] == a[k] for k in range(n))

    primes = list(primes)
    best = None
    for r in range(1, len(primes) + 1):
        for combo in itertools.combinations(primes, r):
            if all(any(covers(p, a) for p in combo) for a in ones):
                best = combo
                break
        if best or r > 6:
            break
    if best is None:  # greedy fallback
        left, best = set(map(tuple, ones)), []
        while left:
            p = max(primes, key=lambda p: sum(covers(p, a) for a in left))
            best.append(p)
            left = {a for a in left if not covers(p, a)}
    return [{vars_[k]: p[k] for k in range(n) if p[k] is not None} for p in best]


def dnf_to_expr(terms: list[dict]) -> str | None:
    if not terms:
        return "false"
    if terms == [{}]:
        return None
    parts = []
    for t in terms:
        lits = [f"{v} == {val}" for v, val in sorted(t.items())]
        parts.append(" and ".join(lits) if len(lits) == 1 or len(terms) == 1 else "(" + " and ".join(lits) + ")")
    return " or ".join(parts)


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------

class Frame:
    def __init__(self, kind: str, line: int, file: str):
        self.kind = kind
        self.line = line
        self.file = file
        self.branches: list = []      # list of CExpr (or None for else)
        self.end = None
        self.guard_of = None          # option name when this is `#ifndef OPTION`
        self.region_ids: list = []    # one region id per branch

    def current(self, env, defined, idx: int) -> bool:
        prev = self.branches[:idx]
        cur = self.branches[idx]
        if any(bool(c_eval(p.ast, env, defined)) for p in prev if p is not None):
            return False
        return True if cur is None else bool(c_eval(cur.ast, env, defined))


def snap(stack: list) -> list:
    """Freeze the condition stack: (frame, active branch index) pairs."""
    return [(fr, len(fr.branches) - 1) for fr in stack]


def _literal(s: str):
    s = s.strip()
    if re.fullmatch(r"-?\d+[uUlL]*", s):
        return int(re.sub(r"[uUlL]+$", "", s)), "int"
    if re.fullmatch(r"0[xX][0-9a-fA-F]+[uUlL]*", s):
        return int(re.sub(r"[uUlL]+$", "", s), 16), "int"
    if re.fullmatch(r'"(?:[^"\\]|\\.)*"', s):
        return json.loads(s), "string"
    return s, "other"


def scan(root: Path, files: list[str]) -> dict:
    # ---------- pass 1: candidate config macros (#ifndef X / #define X lit) ----------
    sources = {}
    for f in files:
        text = (root / f).read_text(errors="replace")
        code, comments = strip_comments(text)
        sources[f] = (code, comments)

    options, excluded = {}, []
    for f, (code, _) in sources.items():
        directives = [(i + 1, DIRECTIVE.match(l)) for i, l in enumerate(code)]
        directives = [(ln, m) for ln, m in directives if m]
        for idx, (ln, m) in enumerate(directives):
            if m.group(1) != "ifndef":
                continue
            name = m.group(2).strip()
            if idx + 1 >= len(directives):
                continue
            ln2, m2 = directives[idx + 1]
            if m2.group(1) == "define" and m2.group(2).strip().split(None, 1)[0] == name:
                rest = m2.group(2).strip().split(None, 1)
                if len(rest) == 1:
                    continue  # include guard: no value
                val, kind = _literal(rest[1])
                end = None
                if idx + 2 < len(directives) and directives[idx + 2][1].group(1) == "endif":
                    end = directives[idx + 2][0]
                if kind == "other":
                    excluded.append({"option_name": name, "reason": f"default is not a literal: {rest[1]!r}",
                                     "evidence": [{"file": f, "lines": f"{ln}-{ln2}", "kind": "definition"}]})
                    continue
                options[name] = {"name": name, "default": val, "literal_kind": kind,
                                 "definition": {"file": f, "lines": [ln, ln2] + ([end] if end else [])},
                                 "uses": [], "code_uses": [], "directive_uses": []}
            else:
                # #ifndef X whose body defines X in a platform-dependent way
                body_defines = [d for d in directives[idx + 1:idx + 40]
                                if d[1].group(1) == "define" and d[1].group(2).strip().split(None, 1)[0] == name]
                if body_defines:
                    excluded.append({"option_name": name,
                                     "reason": "overridable macro with platform-dependent definitions (not a literal default)",
                                     "evidence": [{"file": f, "lines": f"{ln}", "kind": "definition"}]})

    bool_like = {n for n, o in options.items() if o["literal_kind"] == "int" and o["default"] in (0, 1)}

    # ---------- pass 2: walk directives with a condition stack ----------
    regions = []          # {id, file, start, end, cond_ids, cond_text, uses:set}
    use_records = []      # (option, kind, file, line, stack snapshot, expr or None)
    constraint_cands = []
    leads = []

    for f, (code, comments) in sources.items():
        stack: list[Frame] = []
        cont = ""
        cont_line = None
        for i, raw in enumerate(code):
            ln = i + 1
            line = raw
            if cont:
                line = cont + " " + raw.strip()
            if line.rstrip().endswith("\\"):
                cont = line.rstrip()[:-1]
                cont_line = cont_line or ln
                continue
            dl = cont_line or ln
            cont, cont_line = "", None
            m = DIRECTIVE.match(line)
            if m:
                kind, arg = m.group(1), m.group(2).strip()
                if kind in ("if", "ifdef", "ifndef"):
                    fr = Frame(kind, dl, f)
                    if kind == "if":
                        e = CExpr(arg)
                    elif kind == "ifdef":
                        e = CExpr(f"defined({arg.split()[0]})")
                    else:
                        e = CExpr(f"!defined({arg.split()[0]})")
                        if arg.split()[0] in options:
                            fr.guard_of = arg.split()[0]
                    # directive uses: options referenced in the expression
                    for name in e.ids() & set(options):
                        if kind == "if":
                            use_records.append((name, "directive", f, dl, snap(stack), e, []))
                    fr.branches.append(e)
                    stack.append(fr)
                    rid = len(regions)
                    regions.append({"id": rid, "file": f, "start": dl, "end": None, "frame": fr,
                                    "branch": 0, "uses": set(), "stack": snap(stack)})
                    fr.region_ids.append(rid)
                elif kind in ("elif", "else"):
                    fr = stack[-1]
                    regions[fr.region_ids[-1]]["end"] = dl - 1
                    if kind == "elif":
                        e = CExpr(arg)
                        for name in e.ids() & set(options):
                            use_records.append((name, "directive", f, dl, snap(stack[:-1]), e,
                                                list(fr.branches)))
                        fr.branches.append(e)
                    else:
                        fr.branches.append(None)
                    rid = len(regions)
                    regions.append({"id": rid, "file": f, "start": dl, "end": None, "frame": fr,
                                    "branch": len(fr.branches) - 1, "uses": set(), "stack": snap(stack)})
                    fr.region_ids.append(rid)
                elif kind == "endif":
                    fr = stack.pop()
                    fr.end = dl
                    regions[fr.region_ids[-1]]["end"] = dl - 1
                elif kind == "error":
                    constraint_cands.append({"file": f, "line": dl, "message": arg, "stack": snap(stack)})
                elif kind == "define":
                    name = arg.split(None, 1)[0].split("(")[0]
                    body = arg[len(name):]
                    in_guard = any(fr.guard_of == name for fr in stack)
                    if not in_guard:
                        for opt in set(IDENT.findall(body)) & set(options):
                            use_records.append((opt, "code", f, dl, snap(stack), None, []))
                continue
            # ordinary code line
            for opt in set(IDENT.findall(line)) & set(options):
                use_records.append((opt, "code", f, ln, snap(stack), None, []))
                for fr in stack:
                    regions[fr.region_ids[-1]]["uses"].add(opt)
        # comment leads
        for cl, ctext in comments.items():
            mentioned = set(IDENT.findall(ctext)) & set(options)
            # a doc comment documents the option whose #ifndef follows it
            k = cl - 1 + ctext.count("\n") + 1
            while k < len(code) and not code[k].strip():
                k += 1
            nxt = DIRECTIVE.match(code[k]) if k < len(code) else None
            if nxt and nxt.group(1) == "ifndef" and nxt.group(2).strip() in options:
                mentioned.add(nxt.group(2).strip())
                options[nxt.group(2).strip()].setdefault("doc", " ".join(ctext.split()))
                options[nxt.group(2).strip()].setdefault("doc_lines", [cl, cl + ctext.count("\n")])
            mentioned = sorted(mentioned)
            if len(mentioned) >= 2:
                leads.append({"kind": "comment_co_mention", "file": f, "line": cl, "options": mentioned,
                              "text": " ".join(ctext.split())[:400]})

    # ---------- presence conditions ----------
    def frame_vars(stack):
        vs = set()
        for fr, idx in stack:
            for b in fr.branches[: idx + 1]:
                if b is not None:
                    vs |= b.ids()
        return vs

    def stack_true(stack, env, defined):
        return all(fr.current(env, defined, idx) for fr, idx in stack)

    def make_env(assign: dict):
        env = dict(HOST_PROFILE)
        defined = set(HOST_PROFILE) | set(options)
        for n, o in options.items():
            env[n] = o["default"] if o["literal_kind"] == "int" else 1
        env.update(assign)
        return env, defined

    per_option = {n: [] for n in options}
    for (opt, kind, f, ln, stack, expr, prev_branches) in use_records:
        if any(fr.guard_of == opt for fr, _ in stack):
            continue  # its own definition
        per_option[opt].append((kind, f, ln, stack, expr, prev_branches))

    for name, o in options.items():
        uses = per_option[name]
        o["uses"] = [{"file": f, "line": ln, "kind": k} for (k, f, ln, *_r) in uses]
        vs = set()
        for (k, f, ln, stack, expr, prev) in uses:
            vs |= frame_vars(stack)
            if expr is not None:
                vs |= expr.ids()
            for p in prev:
                vs |= p.ids()
        vs = sorted((vs & bool_like) | ({name} if name in bool_like else set()))
        if not uses:
            o["active_when"] = "false"
            o["dead"] = True
            continue
        truth = {}
        others = [v for v in vs if v != name]
        for bits in itertools.product((0, 1), repeat=len(others)):
            assign = dict(zip(others, bits))
            live = False
            for (k, f, ln, stack, expr, prev) in uses:
                if k == "code":
                    env, defined = make_env(assign)
                    if stack_true(stack, env, defined):
                        live = True
                        break
                else:
                    # Boolean difference of the branch selection w.r.t. this option
                    vals = []
                    for xv in ((0, 1) if name in bool_like else (o["default"], o["default"] + 1)):
                        env, defined = make_env({**assign, name: xv})
                        if not stack_true(stack, env, defined):
                            vals.append(None)
                            continue
                        if any(bool(c_eval(p.ast, env, defined)) for p in prev if p is not None):
                            vals.append(None)
                            continue
                        vals.append(bool(c_eval(expr.ast, env, defined)))
                    if None not in vals and vals[0] != vals[1]:
                        live = True
                        break
            truth[bits] = live
        terms = minimise(others, truth) if others else ([{}] if truth.get((), False) else [])
        o["active_when"] = dnf_to_expr(terms)
        o["condition_vars"] = others

    # ---------- domain type inference ----------
    for name, o in options.items():
        if o["literal_kind"] == "string":
            o["type"] = "string"
            continue
        if name in bool_like:
            numeric_use = False
            for (k, f, ln, stack, expr, prev) in per_option[name]:
                if k == "code":
                    line = sources[f][0][ln - 1]
                    if re.search(rf"\[\s*{name}\s*\]|{name}\s*[-+*/]|[-+*/<>]\s*{name}\b", line):
                        numeric_use = True
            o["type"] = "integer" if numeric_use else "boolean"
        else:
            o["type"] = "integer"

    # ---------- interaction-group candidates ----------
    raw_groups = []
    for r in regions:
        cond_opts = set()
        nested = False
        for fr, idx in r["stack"]:
            if fr.guard_of:
                continue
            bs = [b for b in fr.branches[: idx + 1] if b is not None]
            ids = set()
            for b in bs:
                ids |= b.ids()
            ids &= set(options)
            if ids and fr is not r["frame"]:
                nested = True
            cond_opts |= ids
        own = set()
        for b in r["frame"].branches[: r["branch"] + 1]:
            if b is not None:
                own |= b.ids() & set(options)
        members = cond_opts | r["uses"]
        if len(members) >= 2 and own:
            reason = "nested_ifdef" if nested else ("shared_condition" if len(own) >= 2 else "shared_code")
            raw_groups.append({"options": members, "reason": reason, "file": r["file"],
                               "lines": [r["start"], r["end"] or r["start"]]})
    # merge groups that share >= 2 options (transitively)
    merged = []
    for g in raw_groups:
        g = {"options": set(g["options"]), "reasons": {g["reason"]}, "regions": [(g["file"], g["lines"])]}
        changed = True
        while changed:
            changed = False
            for h in list(merged):
                if len(h["options"] & g["options"]) >= 2:
                    merged.remove(h)
                    g["options"] |= h["options"]
                    g["reasons"] |= h["reasons"]
                    g["regions"] += h["regions"]
                    changed = True
        merged.append(g)
    groups = []
    for i, g in enumerate(merged):
        lines = []
        files_ = set()
        for f, (a, b) in g["regions"]:
            lines += list(range(a, b + 1))
            files_.add(f)
        reason = "nested_ifdef" if "nested_ifdef" in g["reasons"] else sorted(g["reasons"])[0]
        groups.append({"id": f"S1_G{i + 1}", "options": sorted(g["options"]), "reason": reason,
                       "region": {"file": sorted(files_)[0], "lines": lines},
                       "suggested_strength": min(3, len(g["options"]))})

    # ---------- #error candidates ----------
    cands = []
    for c in constraint_cands:
        vs = sorted(frame_vars(c["stack"]) & set(options))
        cands.append({"file": c["file"], "line": c["line"], "message": c["message"], "options": vs,
                      "note": "configurations reaching this #error are invalid (hard constraint)"})

    for o in options.values():
        o.pop("code_uses", None)
        o.pop("directive_uses", None)
    return {"stage": "S1_cpp", "root": str(root), "files": files, "host_profile": HOST_PROFILE,
            "options": list(options.values()), "excluded": excluded, "groups": groups,
            "constraint_candidates": cands, "leads": leads}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", required=True, help="subject checkout")
    ap.add_argument("files", nargs="+", help="source/header files relative to root")
    ap.add_argument("-o", "--output")
    a = ap.parse_args(argv)
    res = scan(Path(a.root), a.files)
    text = json.dumps(res, indent=2, default=list)
    if a.output:
        Path(a.output).write_text(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
