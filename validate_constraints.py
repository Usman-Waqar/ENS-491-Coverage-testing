#!/usr/bin/env python3
"""Validate candidate configurations against a Configuration Space Model (csm.json).

A configuration is a dict {option_name: value}. Missing options take their
default_value. Validation checks, in order:

  1. unknown option names                         -> error
  2. domain: value type / discrete settings / valid_range
       outside discrete_settings but inside valid_range -> warning (off-partition)
       outside valid_range or not an allowed enum value  -> error
  3. constraints: each constraint formula must hold
       severity 'hard' violated -> error (configuration invalid)
       severity 'soft' violated -> warning (or error with --strict)
  4. inert options: options whose 'active_when' is false are reported; the
     'effective' configuration collapses them to their default, which is what
     build_and_test.py uses to decide whether a rebuild is needed.

Constraints are evaluated from 'formula' when present, otherwise parsed from
'expression'. `--self-test` checks the CSM against csm_schema.json and checks
that every formula agrees with its expression on random configurations.

Usage:
  validate_constraints.py --config '{"INI_CUSTOM_ALLOCATOR": 1}'
  validate_constraints.py --config-file configs.json      (object or list)
  validate_constraints.py --default
  validate_constraints.py --self-test
Exit status: 0 if every configuration is valid, 1 otherwise, 2 on usage error.

Also importable: load_csm(), validate(), effective_config(), sample_random(),
sample_each_choice(), eval_condition(), parse_expression().
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_CSM = HERE / "csm.json"
DEFAULT_SCHEMA = HERE / "csm_schema.json"


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

class CSM:
    """Thin wrapper around the parsed csm.json with lookup helpers."""

    def __init__(self, data: dict, path: Path):
        self.data = data
        self.path = path
        self.options = {o["option_name"]: o for o in data["options"]}
        self.order = [o["option_name"] for o in data["options"]]
        self.constraints = data.get("constraints", [])
        self._compiled = {}
        for c in self.constraints:
            formula = c.get("formula")
            if formula is None:
                formula = parse_expression(c["expression"], self.options)
            self._compiled[c["id"]] = formula
        for name, o in self.options.items():
            if "active_when" in o:
                _check_refs(o["active_when"], self.options, f"active_when of {name}")
        for cid, f in self._compiled.items():
            _check_refs(f, self.options, f"constraint {cid}")

    def formula(self, cid: str) -> Any:
        return self._compiled[cid]

    def compile_time_options(self) -> list[str]:
        return [n for n in self.order if self.options[n]["binding_time"] == "compile_time"]

    def runtime_options(self) -> list[str]:
        return [n for n in self.order if self.options[n]["binding_time"] == "runtime"]

    def defaults(self) -> dict:
        return {n: self.options[n]["default_value"] for n in self.order}


def load_csm(path: str | Path = DEFAULT_CSM, schema: str | Path | None = DEFAULT_SCHEMA) -> CSM:
    path = Path(path)
    data = json.loads(path.read_text())
    if schema is not None and Path(schema).exists():
        try:
            import jsonschema  # type: ignore
        except ImportError:  # schema check is best effort
            jsonschema = None
        if jsonschema is not None:
            jsonschema.validate(data, json.loads(Path(schema).read_text()))
    return CSM(data, path)


def _check_refs(cond: Any, options: dict, where: str) -> None:
    if isinstance(cond, bool):
        return
    for key in ("all", "any"):
        if key in cond:
            for c in cond[key]:
                _check_refs(c, options, where)
            return
    if "not" in cond:
        _check_refs(cond["not"], options, where)
        return
    if "implies" in cond:
        for c in cond["implies"]:
            _check_refs(c, options, where)
        return
    for k in ("option", "ref"):
        if k in cond and cond[k] not in options:
            raise ValueError(f"{where}: unknown option '{cond[k]}'")


# --------------------------------------------------------------------------
# Expression language  ->  formula tree
#
#   expr    := 'if' disj 'then' disj | disj
#   disj    := conj ('or' conj)*
#   conj    := neg ('and' neg)*
#   neg     := 'not' neg | '(' expr ')' | 'true' | 'false' | atom
#   atom    := IDENT OP (literal | IDENT)  |  IDENT ('in' | 'not in') '[' literal (',' literal)* ']'
#   literal := integer | float | "string"
# --------------------------------------------------------------------------

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
        if tok == ("kw", "not"):
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
        if name not in self.options:
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
            if val not in self.options:
                raise ValueError(f"unknown option in expression: {val}")
            return {"option": name, "op": op, "ref": val}
        self.take("lit")
        return {"option": name, "op": op, "value": val}


def parse_expression(expression: str, options: dict) -> Any:
    p = _Parser(_tokenize(expression), options)
    tree = p.expr()
    if p.i != len(p.t):
        raise ValueError(f"trailing tokens in expression: {expression!r}")
    return tree


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

_CMP = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


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
    lhs = config[cond["option"]]
    op = cond["op"]
    if op in ("in", "not_in"):
        hit = lhs in cond["values"]
        return hit if op == "in" else not hit
    rhs = config[cond["ref"]] if "ref" in cond else cond["value"]
    try:
        return _CMP[op](lhs, rhs)
    except TypeError:
        return False


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


# --------------------------------------------------------------------------
# Configuration checks
# --------------------------------------------------------------------------

def complete(csm: CSM, config: dict) -> tuple[dict, list[str]]:
    """Fill in defaults; return (full_config, unknown_option_names)."""
    unknown = [k for k in config if k not in csm.options]
    full = csm.defaults()
    full.update({k: v for k, v in config.items() if k in csm.options})
    return full, unknown


def _type_ok(opt: dict, v: Any) -> bool:
    dt = opt["domain_type"]
    if dt == "boolean":
        return isinstance(v, int) and not isinstance(v, bool) and v in (0, 1)
    if dt == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if dt == "string":
        return isinstance(v, str)
    return True  # enum: membership checked below


def check_domain(csm: CSM, config: dict) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    for name in csm.order:
        opt, v = csm.options[name], config[name]
        settings = [s["value"] for s in opt["discrete_settings"]]
        if not _type_ok(opt, v):
            errors.append(f"{name}={v!r}: wrong type for domain '{opt['domain_type']}'")
            continue
        if v in settings:
            continue
        if opt["domain_type"] == "integer":
            rng = opt.get("valid_range", {})
            lo, hi = rng.get("min"), rng.get("max")
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                errors.append(f"{name}={v}: outside valid_range [{lo}, {hi}]")
            else:
                warnings.append(f"{name}={v}: legal but off-partition (settings {settings})")
        elif opt["domain_type"] == "string":
            warnings.append(f"{name}={v!r}: legal string but off-partition (settings {settings})")
        else:
            errors.append(f"{name}={v!r}: not one of {settings}")
    return errors, warnings


def inert_options(csm: CSM, config: dict) -> list[str]:
    return [n for n in csm.order
            if "active_when" in csm.options[n] and not eval_condition(csm.options[n]["active_when"], config)]


def effective_config(csm: CSM, config: dict) -> dict:
    """Collapse inert options to their defaults (iterate to a fixed point)."""
    eff = dict(config)
    for _ in range(len(csm.order) + 1):
        changed = False
        for n in inert_options(csm, eff):
            d = csm.options[n]["default_value"]
            if eff[n] != d:
                eff[n] = d
                changed = True
        if not changed:
            break
    return eff


def validate(csm: CSM, config: dict, strict: bool = False) -> dict:
    full, unknown = complete(csm, config)
    errors = [f"unknown option '{k}'" for k in unknown]
    dom_err, warnings = check_domain(csm, full)
    errors += dom_err
    violated = []
    if not dom_err:
        for c in csm.constraints:
            if not eval_condition(csm.formula(c["id"]), full):
                violated.append({"id": c["id"], "severity": c["severity"], "expression": c["expression"]})
                msg = f"{c['id']} ({c['severity']}) violated: {c['expression']}"
                if c["severity"] == "hard" or strict:
                    errors.append(msg)
                else:
                    warnings.append(msg)
    inert = inert_options(csm, full) if not dom_err else []
    eff = effective_config(csm, full) if not dom_err else full
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "violated_constraints": violated,
        "inert_options": inert,
        "configuration": full,
        "effective_configuration": eff,
    }


# --------------------------------------------------------------------------
# Sampling helpers (used by build_and_test.py)
# --------------------------------------------------------------------------

def _settings(csm: CSM, name: str) -> list:
    return [s["value"] for s in csm.options[name]["discrete_settings"]]


def sample_random(csm: CSM, n: int, seed: int = 0, max_tries: int = 100000) -> list[dict]:
    """n valid configurations drawn uniformly over discrete settings (rejection sampling)."""
    rng = random.Random(seed)
    out, tries = [], 0
    while len(out) < n and tries < max_tries:
        tries += 1
        cfg = {name: rng.choice(_settings(csm, name)) for name in csm.order}
        if validate(csm, cfg)["valid"]:
            out.append(cfg)
    return out


def sample_each_choice(csm: CSM, seed: int = 0) -> list[dict]:
    """Each-choice (1-wise) coverage: every setting of every option appears in
    at least one valid configuration; other options stay at their defaults
    unless a constraint forces a change."""
    rng = random.Random(seed)
    out, seen = [], set()
    for name in csm.order:
        for v in _settings(csm, name):
            if (name, v) in seen:
                continue
            cfg = csm.defaults()
            cfg[name] = v
            if not validate(csm, cfg)["valid"]:
                # repair: try re-drawing the other options until valid
                for _ in range(2000):
                    cand = {o: (v if o == name else rng.choice(_settings(csm, o))) for o in csm.order}
                    if validate(csm, cand)["valid"]:
                        cfg = cand
                        break
                else:
                    continue
            out.append(cfg)
            seen.update((o, cfg[o]) for o in csm.order)
    return out


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def self_test(csm: CSM, samples: int = 5000) -> list[str]:
    problems = []
    rep = validate(csm, {})
    if not rep["valid"]:
        problems.append(f"default configuration is invalid: {rep['errors']}")
    for name, o in csm.options.items():
        settings = [s["value"] for s in o["discrete_settings"]]
        if o["default_value"] not in settings:
            problems.append(f"{name}: default {o['default_value']!r} not in discrete_settings")
        if len(settings) != len(set(map(json.dumps, settings))):
            problems.append(f"{name}: duplicate discrete settings")
    rng = random.Random(1)
    for c in csm.constraints:
        parsed = parse_expression(c["expression"], csm.options)
        if "formula" not in c:
            continue
        for _ in range(samples):
            cfg = {n: rng.choice(_settings(csm, n)) for n in csm.order}
            if eval_condition(parsed, cfg) != eval_condition(c["formula"], cfg):
                problems.append(f"{c['id']}: formula and expression disagree on {cfg}")
                break
    # every hard constraint should exclude something, and not everything
    space = 1
    for n in csm.order:
        space *= len(_settings(csm, n))
    for c in csm.constraints:
        hits = sum(eval_condition(csm.formula(c["id"]), {n: rng.choice(_settings(csm, n)) for n in csm.order})
                   for _ in range(samples))
        if hits == 0:
            problems.append(f"{c['id']}: never satisfied on {samples} random configurations")
        if hits == samples:
            problems.append(f"{c['id']}: never violated on {samples} random configurations (vacuous?)")
    return problems


def _referenced(cond: Any, acc: set) -> set:
    if isinstance(cond, bool):
        return acc
    for key in ("all", "any", "implies"):
        if key in cond:
            for c in cond[key]:
                _referenced(c, acc)
            return acc
    if "not" in cond:
        return _referenced(cond["not"], acc)
    acc.add(cond["option"])
    if "ref" in cond:
        acc.add(cond["ref"])
    return acc


def space_stats(csm: CSM) -> dict:
    """Exact counts over the discretised space.

    Only options mentioned by a constraint or an active_when condition (plus the
    options that own an active_when) are enumerated; every other option is
    independent, so its domain size is a pure multiplier."""
    rel: set = set()
    for c in csm.constraints:
        _referenced(csm.formula(c["id"]), rel)
    for n, o in csm.options.items():
        if "active_when" in o:
            rel.add(n)
            _referenced(o["active_when"], rel)
    rel_names = [n for n in csm.order if n in rel]
    free = [n for n in csm.order if n not in rel]
    mult_all = mult_ct = 1
    for n in free:
        mult_all *= len(_settings(csm, n))
        if csm.options[n]["binding_time"] == "compile_time":
            mult_ct *= len(_settings(csm, n))
    hard = [csm.formula(c["id"]) for c in csm.constraints if c["severity"] == "hard"]
    soft = [csm.formula(c["id"]) for c in csm.constraints if c["severity"] == "soft"]
    base = csm.defaults()
    total = valid = valid_strict = 0
    ct_rel = [n for n in rel_names if csm.options[n]["binding_time"] == "compile_time"]
    raw_builds, eff_builds = set(), set()
    for combo in itertools.product(*[_settings(csm, n) for n in rel_names]):
        cfg = dict(base)
        cfg.update(zip(rel_names, combo))
        total += 1
        if all(eval_condition(f, cfg) for f in hard):
            valid += 1
            if all(eval_condition(f, cfg) for f in soft):
                valid_strict += 1
            raw_builds.add(tuple(cfg[n] for n in ct_rel))
            eff = effective_config(csm, cfg)
            eff_builds.add(tuple(eff[n] for n in ct_rel))
    return {
        "raw_configurations": total * mult_all,
        "valid_configurations": valid * mult_all,
        "valid_configurations_strict": valid_strict * mult_all,
        "valid_compile_time_variants": len(raw_builds) * mult_ct,
        "distinct_effective_builds": len(eff_builds) * mult_ct,
        "enumerated_options": rel_names,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--csm", default=str(DEFAULT_CSM))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--config", help="JSON object of option settings")
    g.add_argument("--config-file", help="JSON file with an object or a list of objects")
    g.add_argument("--default", action="store_true", help="validate the default configuration")
    g.add_argument("--self-test", action="store_true", help="schema + formula/expression consistency checks")
    g.add_argument("--stats", action="store_true", help="enumerate the discretised space (slow-ish)")
    ap.add_argument("--strict", action="store_true", help="treat soft-constraint violations as errors")
    ap.add_argument("--quiet", action="store_true", help="only print the verdict per configuration")
    args = ap.parse_args(argv)

    csm = load_csm(args.csm)

    if args.self_test:
        problems = self_test(csm)
        print(json.dumps({"csm": str(csm.path), "options": len(csm.order),
                          "constraints": len(csm.constraints), "problems": problems}, indent=2))
        return 1 if problems else 0
    if args.stats:
        print(json.dumps(space_stats(csm), indent=2))
        return 0

    if args.default:
        configs = [{}]
    elif args.config is not None:
        configs = [json.loads(args.config)]
    else:
        loaded = json.loads(Path(args.config_file).read_text())
        configs = loaded if isinstance(loaded, list) else [loaded]

    all_ok = True
    for i, cfg in enumerate(configs):
        if not isinstance(cfg, dict):
            print(f"configuration #{i} is not a JSON object", file=sys.stderr)
            return 2
        rep = validate(csm, cfg, strict=args.strict)
        all_ok &= rep["valid"]
        if args.quiet:
            print(f"#{i}: {'VALID' if rep['valid'] else 'INVALID'}"
                  + (f"  errors={rep['errors']}" if rep["errors"] else ""))
        else:
            print(json.dumps({"index": i, **rep}, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
