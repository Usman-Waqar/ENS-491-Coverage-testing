#!/usr/bin/env python3
"""S2 - build-system and documentation scan.

Meson (meson_options.txt + meson.build):
  * declared options: type, default, choices, min/max, description
  * which -D<MACRO>=<value> each option controls, and whether the mapping is
    inverted (boolean option true -> macro 0)
  * masking: -D flags emitted only in one branch of `if <option>` (e.g. all
    feature flags are ignored when distro_install=true)
CMake (option(), set(... CACHE ...)) and autoconf (AC_ARG_ENABLE/WITH) are
recognised at the declaration level for later subjects.

Documentation: every README*/doc line that mentions a candidate macro name is
recorded as documentation evidence (macro names come from S1 or the -D flags).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MESON_OPT = re.compile(r"option\(\s*'([^']+)'(.*?)\)\s*$", re.S | re.M)
KV = re.compile(r"(\w+)\s*:\s*('(?:[^'\\]|\\.)*'|\[[^\]]*\]|true|false|-?\d+)")
DFLAG = re.compile(r"-D([A-Za-z_][A-Za-z0-9_]*)(?:=([^'\"\]\s+]*))?")
GETOPT = re.compile(r"get_option\('([^']+)'\)")


def _val(s: str):
    s = s.strip()
    if s in ("true", "false"):
        return s == "true"
    if s.startswith("'"):
        return s[1:-1]
    if s.startswith("["):
        return [x.strip().strip("'") for x in s[1:-1].split(",") if x.strip()]
    return int(s)


def parse_meson_options(text: str) -> dict:
    out = {}
    for m in re.finditer(r"option\(\s*'([^']+)'(.*?)\n\)", text, re.S):
        name, body = m.group(1), m.group(2)
        kv = {k: _val(v) for k, v in KV.findall(body)}
        line = text[: m.start()].count("\n") + 1
        out[name] = {"type": kv.get("type"), "default": kv.get("value"), "description": kv.get("description"),
                     "choices": kv.get("choices"), "min": kv.get("min"), "max": kv.get("max"), "line": line}
    return out


def parse_meson_build(text: str, options: dict) -> dict:
    """Map -D macros to the meson option that controls them."""
    lines = text.splitlines()
    var_of = {}       # meson variable -> option
    for ln in lines:
        m = re.match(r"\s*(\w+)\s*=\s*get_option\('([^']+)'\)", ln)
        if m:
            var_of[m.group(1)] = m.group(2)
    stack = []        # (indent, kind, text, line_no)
    mapping, masked = {}, []
    for i, ln in enumerate(lines, 1):
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue
        ind = len(ln) - len(ln.lstrip())
        while stack and stack[-1][0] >= ind and not stripped.startswith(("elif", "else", "endif")):
            stack.pop()
        if stripped.startswith("if "):
            stack.append((ind, "if", stripped[3:], i))
            continue
        if stripped.startswith("elif ") or stripped == "else":
            # replace the matching branch at this indentation
            while stack and stack[-1][0] > ind:
                stack.pop()
            if stack and stack[-1][0] == ind:
                prev = stack.pop()
                stack.append((ind, "else" if stripped == "else" else "elif",
                              (stripped[5:] if stripped != "else" else "not (" + prev[2] + ")"), i))
            continue
        if stripped == "endif":
            while stack and stack[-1][0] > ind:
                stack.pop()
            if stack and stack[-1][0] == ind:
                stack.pop()
            continue
        for m in DFLAG.finditer(ln):
            macro, val = m.group(1), m.group(2)
            ctrl, neg, conds = None, False, [s[2] for s in stack]
            # direct use in the same line: '-DX=' + var.to_string()
            for v, opt in var_of.items():
                if re.search(rf"\b{v}\b", ln):
                    ctrl = opt
            if ctrl is None:
                for cond in reversed(conds):
                    g = GETOPT.search(cond)
                    names = [g.group(1)] if g else [opt for v, opt in var_of.items() if re.search(rf"\b{v}\b", cond)]
                    names = [n for n in names if n not in ("distro_install", "default_library")]
                    if names:
                        ctrl = names[0]
                        neg = cond.strip().startswith("not ")
                        break
            masks = [var_of.get(c.replace("not (", "").rstrip(")").strip(), c) for c in conds
                     if re.fullmatch(r"(not \()?\s*\w+\s*\)?", c) and
                     var_of.get(c.replace("not (", "").rstrip(")").strip()) in ("distro_install",)]
            rec = {"macro": macro, "value": val, "line": i, "conditions": conds}
            if ctrl:
                rec["meson_option"] = ctrl
                rec["negated_condition"] = neg
                otype = options.get(ctrl, {}).get("type")
                if otype == "boolean" and val in ("0", "1"):
                    # 'if get_option(x)' -> -DM=val ; inverted when (x true -> M=0)
                    macro_when_true = int(val) if not neg else 1 - int(val)
                    rec["inverted"] = macro_when_true == 0
            if masks:
                rec["masked_by"] = sorted(set(masks))
            mapping.setdefault(macro, []).append(rec)
            if any("distro_install" in c for c in conds):
                masked.append(macro)
    return {"macros": mapping, "masked_when_distro_install": sorted(set(masked)),
            "variables": var_of}


def scan_docs(root: Path, names: list[str]) -> dict:
    hits = {n: [] for n in names}
    docs = [p for p in root.glob("*") if p.is_file() and re.match(r"(?i)readme|.*\.md$|.*\.txt$|.*\.rst$", p.name)]
    docs += [p for p in (root / "doc").glob("**/*") if p.is_file()] if (root / "doc").exists() else []
    for p in docs:
        try:
            for i, ln in enumerate(p.read_text(errors="replace").splitlines(), 1):
                for n in names:
                    if re.search(rf"(?:\b|(?<=-D)){re.escape(n)}\b", ln):
                        hits[n].append({"file": str(p.relative_to(root)), "line": i, "text": ln.strip()[:200]})
        except OSError:
            pass
    return hits


def scan(root: Path, macro_names: list[str]) -> dict:
    out = {"stage": "S2_build", "root": str(root), "meson": None, "cmake": [], "autoconf": [], "docs": {}}
    mo = root / "meson_options.txt"
    mb = root / "meson.build"
    if mo.exists():
        opts = parse_meson_options(mo.read_text())
        mapping = parse_meson_build(mb.read_text(), opts) if mb.exists() else {}
        aliases = {}
        for macro, recs in mapping.get("macros", {}).items():
            for r in recs:
                if r.get("meson_option"):
                    o = opts.get(r["meson_option"], {})
                    aliases[macro] = {"meson_option": r["meson_option"], "inverted": r.get("inverted", False),
                                      "meson_type": o.get("type"), "meson_default": o.get("default"),
                                      "meson_min": o.get("min"), "meson_max": o.get("max"),
                                      "meson_choices": o.get("choices"),
                                      "options_line": o.get("line"), "build_line": r["line"]}
        out["meson"] = {"options": opts, "mapping": mapping, "aliases": aliases}
    for p in root.rglob("CMakeLists.txt"):
        for i, ln in enumerate(p.read_text(errors="replace").splitlines(), 1):
            m = re.match(r"\s*option\(\s*(\w+)\s+\"([^\"]*)\"\s*(\w+)?", ln)
            if m:
                out["cmake"].append({"file": str(p.relative_to(root)), "line": i, "name": m.group(1),
                                     "description": m.group(2), "default": m.group(3)})
    for p in list(root.glob("configure.ac")) + list(root.glob("configure.in")):
        for i, ln in enumerate(p.read_text(errors="replace").splitlines(), 1):
            m = re.search(r"AC_ARG_(ENABLE|WITH)\(\s*\[?([\w-]+)", ln)
            if m:
                out["autoconf"].append({"file": p.name, "line": i, "kind": m.group(1).lower(), "name": m.group(2)})
    names = set(macro_names)
    if out["meson"]:
        names |= set(out["meson"]["mapping"].get("macros", {}))
    out["docs"] = scan_docs(root, sorted(names))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--s1", help="S1 output (macro names)")
    ap.add_argument("-o", "--output")
    a = ap.parse_args(argv)
    names = []
    if a.s1:
        names = [o["name"] for o in json.loads(Path(a.s1).read_text())["options"]]
    res = scan(Path(a.root), names)
    text = json.dumps(res, indent=2)
    if a.output:
        Path(a.output).write_text(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
