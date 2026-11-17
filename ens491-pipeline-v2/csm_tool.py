#!/usr/bin/env python3
"""CSM v2 utility: convert, migrate, check.

  csm_tool.py yaml2json csm.yaml [-o csm.json]     validate the YAML source and write the JSON view
  csm_tool.py json2yaml csm.json [-o csm.yaml]     the reverse (comments are not preserved)
  csm_tool.py migrate  v1.json  [-o csm.yaml]      convert a v1 model to v2 YAML
  csm_tool.py check    csm.yaml|csm.json           schema + self-test (exit 1 on problems)
  csm_tool.py summary  csm.yaml                    one-line-per-option overview

The YAML file is the human-edited source of truth; csm.json is generated from
it and is what the other tools read by default.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import yaml  # noqa: E402

import csm_model as cm  # noqa: E402

_CHAR_NAMES = {";": "SEMI", "#": "HASH", "=": "EQ", ":": "COLON", "[": "LBRACK", "]": "RBRACK",
               "/": "SLASH", "!": "BANG", "%": "PCT", "*": "STAR", " ": "SPACE", ",": "COMMA",
               '"': "DQUOTE", "\\": "BSLASH", "'": "SQUOTE", "-": "DASH", ".": "DOT"}
_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def derive_token(value, index: int) -> str:
    """ACTS-safe token for an enum/string value."""
    s = str(value)
    if _SAFE.match(s):
        return s
    if s == "":
        return "NONE"
    if all(ch in _CHAR_NAMES for ch in s):
        return "_".join(_CHAR_NAMES[ch] for ch in s)
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", s)
    if cleaned and _SAFE.match(cleaned):
        return cleaned
    return f"V{index}"


# --------------------------------------------------------------------------
# YAML formatting
# --------------------------------------------------------------------------

class _Dumper(yaml.SafeDumper):
    pass


def _str_rep(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_rep)


def dump_yaml(data: dict, header: str = "") -> str:
    body = yaml.dump(data, Dumper=_Dumper, sort_keys=False, default_flow_style=None,
                     width=110, allow_unicode=True)
    # blank line between top-level list items of 'options' / 'constraints' for readability
    body = re.sub(r"\n(- name: )", r"\n\n\1", body)
    return (header + "\n" if header else "") + body


# --------------------------------------------------------------------------
# v1 -> v2 migration
# --------------------------------------------------------------------------

def _evidence_v2(locs):
    out = []
    for l in locs or []:
        e = {"file": l["file"]}
        if l.get("lines"):
            e["lines"] = cm.format_lines(l["lines"])
        e["kind"] = l.get("kind", "use")
        if l.get("note"):
            e["note"] = l["note"]
        out.append(e)
    return out


def _anchored_partitions(settings, rng):
    reps = sorted(s["value"] for s in settings)
    by_val = {s["value"]: s for s in settings}
    parts = []
    for i, r in enumerate(reps):
        lo = rng.get("min", r) if i == 0 else r
        hi = (reps[i + 1] - 1) if i + 1 < len(reps) else rng.get("max", r)
        s = by_val[r]
        p = {"label": s.get("label") or f"class{i}", "interval": [lo, hi], "representative": r}
        if s.get("rationale"):
            p["rationale"] = s["rationale"]
        parts.append(p)
    return parts


def migrate_v1(v1: dict) -> dict:
    opts = []
    for o in v1["options"]:
        dt = o["domain_type"]
        dom = {"type": dt}
        if dt == "integer":
            rng = dict(o.get("valid_range", {}))
            dom["range"] = rng
            dom["partitions"] = _anchored_partitions(o["discrete_settings"], rng)
        else:
            vals = []
            for i, s in enumerate(o["discrete_settings"]):
                if dt == "boolean" and set(s) <= {"value", "label"}:
                    vals.append(s["value"])
                    continue
                v = {"value": s["value"]}
                if dt in ("enum", "string"):
                    tok = derive_token(s["value"], i)
                    if tok != s["value"]:
                        v["token"] = tok
                for k in ("label", "rationale"):
                    if s.get(k):
                        v[k] = s[k]
                vals.append(v if len(v) > 1 else s["value"])
            dom["values"] = vals
        if o.get("discretization_rationale"):
            dom["rationale"] = o["discretization_rationale"]
        enc = o["encoding"]
        apply = {"mechanism": enc["mechanism"]}
        for k_from, k_to in (("macro", "macro"), ("value_format", "format"), ("flag", "flag"),
                             ("variable", "variable")):
            if enc.get(k_from):
                apply[k_to] = enc[k_from]
        n = {"name": o["option_name"], "binding": o["binding_time"], "description": o["description"],
             "domain": dom, "default": o["default_value"], "default_source": "code"}
        if "active_when" in o:
            n["active_when"] = cm.condition_to_text(o["active_when"]).strip()
            if n["active_when"].startswith("(") and n["active_when"].endswith(")"):
                n["active_when"] = n["active_when"][1:-1]
        n["apply"] = apply
        n["evidence"] = _evidence_v2(o["source_location"])
        if o.get("build_system_alias"):
            n["build_system_alias"] = o["build_system_alias"]
        opts.append(n)

    cons = []
    for c in v1.get("constraints", []):
        cons.append({"id": c["id"], "kind": c["severity"], "expr": c["expression"],
                     "status": "confirmed", "rationale": c["rationale"],
                     "evidence": _evidence_v2(c["evidence"])})

    suite = json.loads(json.dumps(v1.get("test_suite", {"drivers": [], "variants": []})))
    for d in suite.get("drivers", []):
        for key in ("compatible_when", "baseline_valid_when"):
            if key in d:
                d[key] = cm.condition_to_text(d[key])
        for rule in d.get("extra_link_sources_when", []):
            rule["when"] = cm.condition_to_text(rule["when"])
    for v in suite.get("variants", []):
        if "source_location" in v:
            v["source_location"] = _evidence_v2(v["source_location"])

    excluded = []
    for e in v1.get("excluded_options", []):
        d = {"option_name": e["option_name"], "reason": e["reason"]}
        if e.get("source_location"):
            d["evidence"] = _evidence_v2(e["source_location"])
        excluded.append(d)

    return {"csm_version": "2.0", "system": v1["system"], "options": opts, "constraints": cons,
            "interaction_groups": [], "excluded_options": excluded,
            "build": v1.get("build", {}), "test_suite": suite}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_check(path: str) -> int:
    import validate_constraints as vc
    raw = cm.read_model_file(path)
    try:
        cm.schema_validate(raw)
    except Exception as e:  # noqa: BLE001
        print(f"schema: FAIL\n{e}")
        return 1
    csm = cm.load_csm(path)
    problems = vc.self_test(csm)
    print(json.dumps({"file": path, "csm_version": csm.version, "options": len(csm.order),
                      "constraints_enforced": len(csm.constraints),
                      "constraints_total": len(csm.all_constraints),
                      "interaction_groups": len(csm.groups), "problems": problems}, indent=2))
    return 1 if problems else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("yaml2json", "json2yaml", "migrate", "check", "summary"):
        p = sub.add_parser(name)
        p.add_argument("input")
        p.add_argument("-o", "--output")
    args = ap.parse_args(argv)

    if args.cmd == "check":
        return cmd_check(args.input)

    if args.cmd == "yaml2json":
        raw = cm.read_model_file(args.input)
        cm.schema_validate(raw)
        cm.load_csm(args.input)  # parses every expression
        out = args.output or str(Path(args.input).with_suffix(".json"))
        raw = dict(raw)
        raw.setdefault("$schema", "./csm_schema.json")
        raw = {"$schema": raw.pop("$schema"), **raw}
        Path(out).write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {out}")
        return 0

    if args.cmd == "json2yaml":
        raw = cm.read_model_file(args.input)
        raw.pop("$schema", None)
        out = args.output or str(Path(args.input).with_suffix(".yaml"))
        Path(out).write_text(dump_yaml(raw))
        print(f"wrote {out}")
        return 0

    if args.cmd == "migrate":
        v1 = json.loads(Path(args.input).read_text())
        v2 = migrate_v1(v1)
        cm.schema_validate(v2)
        out = args.output or "csm.yaml"
        Path(out).write_text(dump_yaml(v2))
        print(f"wrote {out}")
        return 0

    if args.cmd == "summary":
        csm = cm.load_csm(args.input)
        for n in csm.order:
            o = csm.options[n]
            aw = f"  [active_when {cm.condition_to_text(o['active_when'])}]" if "active_when" in o else ""
            print(f"{n:34s} {o['binding_time']:12s} {o['domain_type']:8s} default={o['default_value']!r:18s} "
                  f"settings={csm.settings(n)}{aw}")
        for c in csm.all_constraints:
            print(f"{c['id']:4s} {c['severity']:4s} {c.get('status','confirmed'):10s} {c['expression']}")
        for g in csm.groups:
            print(f"{g['id']}: t={g['suggested_strength']} {g['options']}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
