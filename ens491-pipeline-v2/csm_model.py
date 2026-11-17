#!/usr/bin/env python3
"""Core library for Configuration Space Models (CSM v1 and v2).

* Loads a model from YAML (v2 source) or JSON (v1 or generated v2), validates
  it against the matching JSON Schema, and normalises it into one internal
  shape (the v1 field names that every tool in this workspace uses).
* Implements the CSM expression language:

      expr    := 'if' disj 'then' disj | disj
      disj    := conj ('or' conj)*
      conj    := neg ('and' neg)*
      neg     := 'not' neg | '(' expr ')' | 'true' | 'false' | atom
      atom    := IDENT OP (literal | IDENT)
               | IDENT ('in' | 'not in') '[' literal (',' literal)* ']'
      OP      := == != < <= > >=
      literal := integer | float | "string"

  parsed into a JSON condition tree (see csm_schema.json $defs/condition),
  with two-valued evaluation (`eval_condition`), three-valued evaluation over
  partial assignments (`eval3`, used by the generators), negation normal form
  (`to_nnf`, used by the ACTS exporter) and pretty-printing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
SCHEMA_V2 = HERE / "csm_schema.json"
SCHEMA_V1 = HERE / "schemas" / "csm_schema_v1.json"

# ==========================================================================
# Expression language
# ==========================================================================

_TOKEN = re.compile(r"""
    \s*(?:
      (?P<str>"(?:[^"\\]|\\.)*")
     |(?P<num>-?\d+(?:\.\d+)?)
     |(?P<op>==|!=|<=|>=|<|>)
     |(?P<punct>[()\[\],])
     |(?P<word>[A-Za-z_][A-Za-z0-9_]*)
    )""", re.VERBOSE)

_KEYWORDS = {"if", "then", "and", "or", "not", "in", "true", "false"}


def _tokenize(s: str) -> list[tuple[str, Any]]:
    pos, out = 0, []
    s = s.rstrip()
    while pos < len(s):
        m = _TOKEN.match(s, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot tokenize expression at: {s[pos:]!r}")
        pos = m.end()
        kind = m.lastgroup
        text = m.group(kind)
        if kind == "str":
            out.append(("lit", json.loads(text)))
        elif kind == "num":
            out.append(("lit", float(text) if "." in text else int(text)))
        elif kind == "word" and text in _KEYWORDS:
            out.append(("kw", text))
        else:
            out.append((kind, text))
    return out


class _Parser:
    def __init__(self, tokens, options):
        self.t = tokens
        self.i = 0
        self.options = options

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else (None, None)

    def take(self, kind=None, text=None):
        tok = self.peek()
        if tok[0] is None or (kind and tok[0] != kind) or (text is not None and tok[1] != text):
            raise ValueError(f"expected {text or kind}, got {tok[1]!r}")
        self.i += 1
        return tok

    def expr(self):
        if self.peek() == ("kw", "if"):
            self.take()
            a = self.disj()
            self.take("kw", "then")
            b = self.disj()
            return {"implies": [a, b]}
        return self.disj()

    def disj(self):
        items = [self.conj()]
        while self.peek() == ("kw", "or"):
            self.take()
            items.append(self.conj())
        return items[0] if len(items) == 1 else {"any": items}

    def conj(self):
        items = [self.neg()]
        while self.peek() == ("kw", "and"):
            self.take()
            items.append(self.neg())
        return items[0] if len(items) == 1 else {"all": items}

    def neg(self):
        tok = self.peek()
        if tok == ("kw", "not") and self.peek(1) != ("kw", "in"):
            self.take()
            return {"not": self.neg()}
        if tok == ("punct", "("):
            self.take()
            e = self.expr()
            self.take("punct", ")")
            return e
        if tok in (("kw", "true"), ("kw", "false")):
            self.take()
            return tok[1] == "true"
        return self.atom()

    def atom(self):
        _, name = self.take("word")
        if self.options is not None and name not in self.options:
            raise ValueError(f"unknown option in expression: {name}")
        tok = self.peek()
        if tok == ("kw", "in") or (tok == ("kw", "not") and self.peek(1) == ("kw", "in")):
            op = "in"
            if tok[1] == "not":
                self.take()
                op = "not_in"
            self.take("kw", "in")
            self.take("punct", "[")
            values = [self.take("lit")[1]]
            while self.peek() == ("punct", ","):
                self.take()
                values.append(self.take("lit")[1])
            self.take("punct", "]")
            return {"option": name, "op": op, "values": values}
        _, op = self.take("op")
        kind, val = self.peek()
        if kind == "word":
            self.take()
            if self.options is not None and val not in self.options:
                raise ValueError(f"unknown option in expression: {val}")
            return {"option": name, "op": op, "ref": val}
        self.take("lit")
        return {"option": name, "op": op, "value": val}


def parse_expression(expression: str, options: dict | None = None) -> Any:
    """Parse a CSM expression. If `options` is given, identifiers must be options."""
    p = _Parser(_tokenize(expression), options)
    tree = p.expr()
    if p.i != len(p.t):
        raise ValueError(f"trailing tokens in expression: {expression!r}")
    return tree


def as_condition(cond: Any, options: dict | None = None) -> Any:
    """Accept either an expression string or an already-parsed condition tree."""
    if isinstance(cond, str):
        return parse_expression(cond, options)
    return cond


_CMP = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}
NEGATED_OP = {"==": "!=", "!=": "==", "<": ">=", ">=": "<", ">": "<=", "<=": ">", "in": "not_in", "not_in": "in"}


def _atom(cond: dict, lhs: Any, rhs_lookup) -> bool:
    op = cond["op"]
    if op in ("in", "not_in"):
        hit = lhs in cond["values"]
        return hit if op == "in" else not hit
    rhs = rhs_lookup(cond["ref"]) if "ref" in cond else cond["value"]
    try:
        return _CMP[op](lhs, rhs)
    except TypeError:
        return False


def eval_condition(cond: Any, config: dict) -> bool:
    if isinstance(cond, bool):
        return cond
    if "all" in cond:
        return all(eval_condition(c, config) for c in cond["all"])
    if "any" in cond:
        return any(eval_condition(c, config) for c in cond["any"])
    if "not" in cond:
        return not eval_condition(cond["not"], config)
    if "implies" in cond:
        a, b = cond["implies"]
        return (not eval_condition(a, config)) or eval_condition(b, config)
    return _atom(cond, config[cond["option"]], lambda r: config[r])


def eval3(cond: Any, partial: dict) -> bool | None:
    """Kleene three-valued evaluation: None if the result depends on unassigned options."""
    if isinstance(cond, bool):
        return cond
    if "all" in cond:
        res = True
        for c in cond["all"]:
            v = eval3(c, partial)
            if v is False:
                return False
            if v is None:
                res = None
        return res
    if "any" in cond:
        res = False
        for c in cond["any"]:
            v = eval3(c, partial)
            if v is True:
                return True
            if v is None:
                res = None
        return res
    if "not" in cond:
        v = eval3(cond["not"], partial)
        return None if v is None else not v
    if "implies" in cond:
        a, b = cond["implies"]
        va = eval3(a, partial)
        if va is False:
            return True
        vb = eval3(b, partial)
        if vb is True:
            return True
        if va is True and vb is False:
            return False
        return None
    name = cond["option"]
    if name not in partial or ("ref" in cond and cond["ref"] not in partial):
        return None
    return _atom(cond, partial[name], lambda r: partial[r])


def referenced_options(cond: Any, acc: set | None = None) -> set:
    acc = set() if acc is None else acc
    if isinstance(cond, bool):
        return acc
    for key in ("all", "any", "implies"):
        if key in cond:
            for c in cond[key]:
                referenced_options(c, acc)
            return acc
    if "not" in cond:
        return referenced_options(cond["not"], acc)
    acc.add(cond["option"])
    if "ref" in cond:
        acc.add(cond["ref"])
    return acc


def to_nnf(cond: Any, negate: bool = False) -> Any:
    """Negation normal form without 'not' nodes and without 'implies'.

    Negations are pushed into the atoms by flipping the comparison operator,
    so the result uses only 'all', 'any', atoms and booleans."""
    if isinstance(cond, bool):
        return (not cond) if negate else cond
    if "not" in cond:
        return to_nnf(cond["not"], not negate)
    if "implies" in cond:
        a, b = cond["implies"]
        # a => b  ==  not a or b ;  not (a => b) == a and not b
        if negate:
            return {"all": [to_nnf(a, False), to_nnf(b, True)]}
        return {"any": [to_nnf(a, True), to_nnf(b, False)]}
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        if negate:
            key = "any" if key == "all" else "all"
        items = [to_nnf(c, negate) for c in cond.get("all", cond.get("any"))]
        return {key: items}
    atom = dict(cond)
    if negate:
        atom["op"] = NEGATED_OP[atom["op"]]
    return atom


def condition_to_text(cond: Any) -> str:
    if isinstance(cond, bool):
        return "true" if cond else "false"
    if "all" in cond:
        return "(" + " and ".join(condition_to_text(c) for c in cond["all"]) + ")"
    if "any" in cond:
        return "(" + " or ".join(condition_to_text(c) for c in cond["any"]) + ")"
    if "not" in cond:
        return "not " + condition_to_text(cond["not"])
    if "implies" in cond:
        a, b = cond["implies"]
        return f"if {condition_to_text(a)} then {condition_to_text(b)}"
    if cond["op"] in ("in", "not_in"):
        return f"{cond['option']} {'in' if cond['op'] == 'in' else 'not in'} {json.dumps(cond['values'])}"
    rhs = cond["ref"] if "ref" in cond else json.dumps(cond["value"])
    return f"{cond['option']} {cond['op']} {rhs}"


# ==========================================================================
# Line specs ("138-142,146" <-> [138, ..., 142, 146])
# ==========================================================================

def parse_lines(spec: Any) -> list[int]:
    if spec is None:
        return []
    if isinstance(spec, int):
        return [spec]
    if isinstance(spec, list):
        out = []
        for s in spec:
            out += parse_lines(s)
        return out
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def format_lines(lines: Iterable[int]) -> str:
    xs = sorted(set(lines))
    if not xs:
        return ""
    parts, start, prev = [], xs[0], xs[0]
    for x in xs[1:]:
        if x == prev + 1:
            prev = x
            continue
        parts.append(f"{start}-{prev}" if prev > start else str(start))
        start = prev = x
    parts.append(f"{start}-{prev}" if prev > start else str(start))
    return ",".join(parts)


# ==========================================================================
# Loading and normalisation
# ==========================================================================

def read_model_file(path: str | Path) -> dict:
    path = Path(path)
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml  # PyYAML
        return yaml.safe_load(text)
    return json.loads(text)


def model_version(raw: dict) -> int:
    return int(str(raw.get("csm_version", "1.0")).split(".")[0])


def schema_validate(raw: dict) -> None:
    schema_path = SCHEMA_V2 if model_version(raw) >= 2 else SCHEMA_V1
    if not schema_path.exists():
        return
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return
    jsonschema.validate(raw, json.loads(schema_path.read_text()))


_FORMAT = {"int": "int", "bool01": "bool01", "c_string_literal": "c_string_literal", "raw": "raw",
           "string": "c_string_literal", "bool": "bool01"}


def _evidence_v1(ev: list | None) -> list:
    out = []
    for e in ev or []:
        d = {"file": e["file"], "kind": e.get("kind", "use")}
        lines = parse_lines(e.get("lines"))
        if lines:
            d["lines"] = lines
        if e.get("note"):
            d["note"] = e["note"]
        out.append(d)
    return out


def _settings_v2(o: dict) -> list[dict]:
    dom = o["domain"]
    t = dom["type"]
    if t == "integer":
        rng = dom.get("range", {})
        out = []
        for p in dom["partitions"]:
            lo, hi = p["interval"]
            lo = rng.get("min") if lo == "min" else lo
            hi = rng.get("max") if hi == "max" else hi
            s = {"value": p["representative"], "label": p.get("label"), "interval": [lo, hi]}
            if p.get("rationale"):
                s["rationale"] = p["rationale"]
            out.append(s)
        return out
    vals = dom.get("values")
    if vals is None and t == "boolean":
        d = o["default"]
        vals = [d, 1 - d]
    out = []
    for v in vals:
        s = dict(v) if isinstance(v, dict) else {"value": v}
        out.append(s)
    return out


def normalize_v2(raw: dict) -> dict:
    """Map a v2 model onto the internal (v1-named) structure."""
    names = {o["name"]: o for o in raw["options"]}
    options = []
    for o in raw["options"]:
        dom = o["domain"]
        apply = o.get("apply", {})
        enc = {"mechanism": apply.get("mechanism")}
        if apply.get("macro"):
            enc["macro"] = apply["macro"]
        if enc["mechanism"] == "c_macro":
            default_fmt = {"boolean": "bool01", "integer": "int", "string": "c_string_literal",
                           "enum": "raw"}[dom["type"]]
            enc["value_format"] = _FORMAT.get(apply.get("format", default_fmt), default_fmt)
        if apply.get("flag"):
            enc["flag"] = apply["flag"]
        if apply.get("variable"):
            enc["variable"] = apply["variable"]
        n = {
            "option_name": o["name"],
            "binding_time": o["binding"],
            "description": o.get("description", ""),
            "domain_type": dom["type"],
            "discrete_settings": _settings_v2(o),
            "default_value": o["default"],
            "default_source": o.get("default_source", "code"),
            "source_location": _evidence_v1(o.get("evidence")),
            "encoding": enc,
            "provenance": o.get("provenance", {}),
        }
        if dom.get("range"):
            n["valid_range"] = {k: v for k, v in dom["range"].items() if k in ("min", "max", "evidence")}
        if dom.get("rationale"):
            n["discretization_rationale"] = dom["rationale"]
        if o.get("active_when") is not None:
            n["active_when"] = as_condition(o["active_when"], names)
        if o.get("build_system_alias"):
            n["build_system_alias"] = o["build_system_alias"]
        options.append(n)

    constraints = []
    for c in raw.get("constraints", []):
        constraints.append({
            "id": c["id"],
            "severity": c["kind"],
            "expression": c["expr"],
            "formula": parse_expression(c["expr"], names),
            "rationale": c.get("rationale", ""),
            "evidence": _evidence_v1(c.get("evidence")),
            "origin": c.get("origin"),
            "status": c.get("status", "confirmed"),
        })

    suite = json.loads(json.dumps(raw.get("test_suite", {"drivers": [], "variants": []})))
    for d in suite.get("drivers", []):
        for key in ("compatible_when", "baseline_valid_when"):
            if key in d:
                d[key] = as_condition(d[key], names)
        for rule in d.get("extra_link_sources_when", []):
            rule["when"] = as_condition(rule["when"], names)
    for v in suite.get("variants", []):
        if "source_location" in v:
            v["source_location"] = _evidence_v1(v["source_location"])

    groups = []
    for g in raw.get("interaction_groups", []):
        g2 = dict(g)
        g2["evidence"] = _evidence_v1(g.get("evidence"))
        groups.append(g2)

    return {
        "csm_version": raw["csm_version"],
        "system": raw["system"],
        "options": options,
        "constraints": constraints,
        "interaction_groups": groups,
        "excluded_options": raw.get("excluded_options", []),
        "build": raw.get("build", {}),
        "test_suite": suite,
    }


class CSM:
    """A loaded, normalised model with lookup helpers.

    `constraints` holds the constraints the tools enforce: status 'refuted' is
    always dropped; status 'hypothesis' is dropped unless include_hypotheses."""

    def __init__(self, data: dict, path: Path, raw: dict | None = None, include_hypotheses: bool = False):
        self.data = data
        self.raw = raw if raw is not None else data
        self.path = Path(path)
        self.version = model_version(self.raw)
        self.options = {o["option_name"]: o for o in data["options"]}
        self.order = [o["option_name"] for o in data["options"]]
        self.all_constraints = data.get("constraints", [])
        self.constraints = [c for c in self.all_constraints
                            if c.get("status", "confirmed") != "refuted"
                            and (include_hypotheses or c.get("status", "confirmed") != "hypothesis")]
        self.groups = data.get("interaction_groups", [])
        self._compiled = {}
        for c in self.all_constraints:
            f = c.get("formula")
            if f is None:
                f = parse_expression(c["expression"], self.options)
            self._compiled[c["id"]] = f
        for name, o in self.options.items():
            if "active_when" in o:
                o["active_when"] = as_condition(o["active_when"], self.options)
                self._check_refs(o["active_when"], f"active_when of {name}")
        for cid, f in self._compiled.items():
            self._check_refs(f, f"constraint {cid}")

    def _check_refs(self, cond, where):
        for n in referenced_options(cond):
            if n not in self.options:
                raise ValueError(f"{where}: unknown option '{n}'")

    # --- lookups -----------------------------------------------------------
    def formula(self, cid: str) -> Any:
        return self._compiled[cid]

    def compile_time_options(self) -> list[str]:
        return [n for n in self.order if self.options[n]["binding_time"] == "compile_time"]

    def runtime_options(self) -> list[str]:
        return [n for n in self.order if self.options[n]["binding_time"] == "runtime"]

    def defaults(self) -> dict:
        return {n: self.options[n]["default_value"] for n in self.order}

    def settings(self, name: str) -> list:
        return [s["value"] for s in self.options[name]["discrete_settings"]]

    def setting_record(self, name: str, value: Any) -> dict | None:
        for s in self.options[name]["discrete_settings"]:
            if s["value"] == value:
                return s
        return None

    def partition_of(self, name: str, value: Any) -> dict | None:
        """The discrete setting whose class contains `value` (exact match first)."""
        rec = self.setting_record(name, value)
        if rec is not None:
            return rec
        o = self.options[name]
        if o["domain_type"] == "integer" and isinstance(value, int):
            for s in o["discrete_settings"]:
                iv = s.get("interval")
                if iv and iv[0] <= value <= iv[1]:
                    return s
        return None

    def representative(self, name: str, value: Any) -> Any:
        rec = self.partition_of(name, value)
        return rec["value"] if rec else None


def load_csm(path: str | Path | None = None, include_hypotheses: bool = False,
             validate_schema: bool = True) -> CSM:
    """Load csm.yaml / csm.json (v1 or v2)."""
    if path is None:
        path = HERE / "csm.json"
    raw = read_model_file(path)
    if validate_schema:
        schema_validate(raw)
    data = normalize_v2(raw) if model_version(raw) >= 2 else raw
    return CSM(data, Path(path), raw=raw, include_hypotheses=include_hypotheses)
