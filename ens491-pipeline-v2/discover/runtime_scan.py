#!/usr/bin/env python3
"""S3 - runtime-option seeds (static leads for the LLM stage).

Runtime variability is spread through code (proposal §2.1), so this stage only
proposes *candidates* with status 'hypothesis':

  * alternative entry points: exported functions that accept the same callback
    type -> an enum option "entry_point" whose values are the function names
  * callback contracts: a callback typedef returning int -> an option
    "<typedef>_return" (the client's return value steers library behaviour)
  * getenv("NAME")                       -> env_var option
  * getopt / getopt_long option strings  -> CLI options
  * argparse add_argument('--x')         -> CLI options (Python subjects)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cpp_scan import strip_comments  # noqa: E402

PROTO = re.compile(r"^\s*(?:\w+\s+)*?(?P<ret>[\w\s\*]+?)\s*\b(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^;{]*)\)\s*;", re.M)
CB_TYPEDEF = re.compile(r"typedef\s+(?P<ret>[\w\s\*]+?)\s*\(\s*\*\s*(?P<name>\w+)\s*\)\s*\((?P<params>[^)]*)\)\s*;")


def scan(root: Path, headers: list[str], sources: list[str]) -> dict:
    cands, leads = [], []
    callback_types = {}
    protos = []
    for h in headers:
        text = (root / h).read_text(errors="replace")
        code_lines, _ = strip_comments(text)
        code = "\n".join(code_lines)
        for m in CB_TYPEDEF.finditer(code):
            line = code[: m.start()].count("\n") + 1
            callback_types.setdefault(m.group("name"), []).append(
                {"file": h, "line": line, "ret": " ".join(m.group("ret").split())})
        for m in PROTO.finditer(code):
            name = m.group("name")
            if "typedef" in m.group(0) or "(*" in m.group(0).replace(" ", ""):
                continue
            if name in ("if", "while", "for", "return", "sizeof", "int", "char", "void", "long", "short",
                        "unsigned", "signed", "float", "double", "size_t") or name in callback_types:
                continue
            line = code[: m.start("name")].count("\n") + 1
            protos.append({"name": name, "params": " ".join(m.group("params").split()), "file": h, "line": line})

    for cb, defs in callback_types.items():
        users = [p for p in protos if re.search(rf"\b{cb}\b", p["params"])]
        if len(users) >= 2:
            names = [p["name"] for p in users]
            default = min(names, key=len)
            cands.append({
                "name": "entry_point", "binding": "runtime", "type": "enum", "values": names,
                "default": default, "default_source": "convention",
                "description": f"Which public function the client calls; all accept a '{cb}' callback.",
                "evidence": [{"file": p["file"], "lines": str(p["line"]), "kind": "definition"} for p in users],
                "rule": "alternative_entry_points"})
        if any(d["ret"] == "int" for d in defs):
            cands.append({
                "name": f"{cb}_return", "binding": "runtime", "type": "enum", "values": ["nonzero", "zero"],
                "default": "nonzero", "default_source": "convention",
                "description": f"Value returned by the client's '{cb}' callback; the library reacts to it.",
                "evidence": [{"file": d["file"], "lines": str(d["line"]), "kind": "definition"} for d in defs],
                "rule": "callback_return_contract"})

    for s in sources:
        text = (root / s).read_text(errors="replace")
        for i, ln in enumerate(text.splitlines(), 1):
            for m in re.finditer(r"getenv\(\s*\"([^\"]+)\"", ln):
                cands.append({"name": m.group(1), "binding": "runtime", "type": "string", "values": [],
                              "evidence": [{"file": s, "lines": str(i), "kind": "use"}], "rule": "getenv",
                              "apply": {"mechanism": "env_var", "variable": m.group(1)}})
            for m in re.finditer(r"getopt(?:_long)?\([^,]+,[^,]+,\s*\"([^\"]+)\"", ln):
                for opt in re.findall(r"([A-Za-z0-9])(:{0,2})", m.group(1)):
                    cands.append({"name": f"cli_{opt[0]}", "binding": "runtime",
                                  "type": "string" if opt[1] else "boolean", "values": [],
                                  "evidence": [{"file": s, "lines": str(i), "kind": "use"}], "rule": "getopt",
                                  "apply": {"mechanism": "driver_cli", "flag": f"-{opt[0]}"}})
            for m in re.finditer(r"add_argument\(\s*['\"](--[\w-]+)['\"]", ln):
                cands.append({"name": m.group(1).lstrip("-").replace("-", "_"), "binding": "runtime",
                              "type": "string", "values": [],
                              "evidence": [{"file": s, "lines": str(i), "kind": "use"}], "rule": "argparse",
                              "apply": {"mechanism": "driver_cli", "flag": m.group(1)}})
    leads.append({"kind": "public_api", "functions": protos})
    return {"stage": "S3_runtime", "candidates": cands, "leads": leads}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--headers", nargs="+", required=True)
    ap.add_argument("--sources", nargs="*", default=[])
    ap.add_argument("-o", "--output")
    a = ap.parse_args(argv)
    res = scan(Path(a.root), a.headers, a.sources)
    text = json.dumps(res, indent=2)
    if a.output:
        Path(a.output).write_text(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
