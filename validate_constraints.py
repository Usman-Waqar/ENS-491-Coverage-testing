#!/usr/bin/env python3
"""Validate candidate configurations against a Configuration Space Model.

Works with csm.yaml / csm.json in format v2 (and legacy v1). A configuration is
a dict {option_name: value}; missing options take their default. Checks:

  1. unknown option names                                   -> error
  2. domain: type, enum membership, integer valid range
       integer value inside a partition interval but not its representative
                                                            -> warning (maps to class)
       outside the valid range / not an allowed enum value  -> error
  3. constraints (status 'refuted' ignored; 'hypothesis' ignored unless
     --include-hypotheses):  hard violated -> error; soft violated -> warning
     (error with --strict)
  4. inert options: options whose 'active_when' is false; the effective
     configuration collapses them to their defaults (equivalent build).

Usage:
  validate_constraints.py --config '{"INI_CUSTOM_ALLOCATOR": 1}'
  validate_constraints.py --config-file configs.json      (object or list)
  validate_constraints.py --default
  validate_constraints.py --self-test
  validate_constraints.py --stats
Exit status: 0 if every configuration is valid, 1 otherwise, 2 on usage error.
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
sys.path.insert(0, str(HERE))

from csm_model import (  # noqa: E402,F401  (re-exported for older importers)
    CSM, as_condition, condition_to_text, eval3, eval_condition, load_csm as _load,
    parse_expression, referenced_options, to_nnf,
)

DEFAULT_CSM = HERE / "csm.json"
ACTS_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_csm(path: str | Path = DEFAULT_CSM, schema: Any = None, include_hypotheses: bool = False) -> CSM:
    """Compatibility wrapper (the `schema` argument is ignored; the schema is chosen by version)."""
    return _load(path, include_hypotheses=include_hypotheses)


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
        settings = csm.settings(name)
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
                continue
            part = csm.partition_of(name, v)
            if part is not None:
                warnings.append(f"{name}={v}: maps to partition '{part.get('label')}' "
                                f"(representative {part['value']})")
            else:
                warnings.append(f"{name}={v}: legal but outside every partition (settings {settings})")
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


def to_representatives(csm: CSM, config: dict) -> tuple[dict, list[str]]:
    """Map every value onto its partition representative (for interaction counting)."""
    out, notes = dict(config), []
    for n, v in config.items():
        if n in csm.options and v not in csm.settings(n):
            rep = csm.representative(n, v)
            if rep is not None:
                out[n] = rep
                notes.append(f"{n}: {v!r} -> {rep!r}")
    return out, notes


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
    return csm.settings(name)


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
    """Each-choice (1-wise): every setting of every option appears in some valid configuration."""
    rng = random.Random(seed)
    out, seen = [], set()
    for name in csm.order:
        for v in _settings(csm, name):
            if (name, v) in seen:
                continue
            cfg = csm.defaults()
            cfg[name] = v
            if not validate(csm, cfg)["valid"]:
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
        settings = csm.settings(name)
        if o["default_value"] not in settings:
            problems.append(f"{name}: default {o['default_value']!r} is not a generation setting")
        if len(settings) != len(set(map(json.dumps, settings))):
            problems.append(f"{name}: duplicate discrete settings")
        # v2: integer partitions must be disjoint, ordered and cover the valid range
        ivs = [s.get("interval") for s in o["discrete_settings"]]
        if o["domain_type"] == "integer" and all(ivs):
            rng = o.get("valid_range", {})
            ivs_sorted = sorted(ivs)
            for s in o["discrete_settings"]:
                lo, hi = s["interval"]
                if not lo <= s["value"] <= hi:
                    problems.append(f"{name}: representative {s['value']} outside its interval {s['interval']}")
            if rng.get("min") is not None and ivs_sorted[0][0] != rng["min"]:
                problems.append(f"{name}: partitions start at {ivs_sorted[0][0]}, range min is {rng['min']}")
            if rng.get("max") is not None and ivs_sorted[-1][1] != rng["max"]:
                problems.append(f"{name}: partitions end at {ivs_sorted[-1][1]}, range max is {rng['max']}")
            for a, b in zip(ivs_sorted, ivs_sorted[1:]):
                if b[0] != a[1] + 1:
                    problems.append(f"{name}: partitions {a} and {b} leave a gap or overlap")
        # v2: ACTS tokens must be unique, identifier-safe
        toks = [s.get("token") for s in o["discrete_settings"] if s.get("token") is not None]
        if len(toks) != len(set(toks)):
            problems.append(f"{name}: duplicate tokens {toks}")
        for t in toks:
            if not ACTS_SAFE.match(str(t)):
                problems.append(f"{name}: token {t!r} is not ACTS-safe")
    rng_ = random.Random(1)
    for c in csm.all_constraints:
        parsed = parse_expression(c["expression"], csm.options)
        f = c.get("formula")
        if f is None:
            continue
        for _ in range(samples):
            cfg = {n: rng_.choice(_settings(csm, n)) for n in csm.order}
            if eval_condition(parsed, cfg) != eval_condition(f, cfg):
                problems.append(f"{c['id']}: formula and expression disagree on {cfg}")
                break
    for c in csm.constraints:
        hits = sum(eval_condition(csm.formula(c["id"]), {n: rng_.choice(_settings(csm, n)) for n in csm.order})
                   for _ in range(samples))
        if hits == 0:
            problems.append(f"{c['id']}: never satisfied on {samples} random configurations")
        if hits == samples:
            problems.append(f"{c['id']}: never violated on {samples} random configurations (vacuous?)")
    for g in csm.groups:
        for n in g.get("options", []):
            if n not in csm.options:
                problems.append(f"interaction group {g.get('id')}: unknown option {n}")
    return problems


def space_stats(csm: CSM) -> dict:
    """Exact counts over the discretised space.

    Only options mentioned by a constraint or an active_when condition (plus the
    options that own an active_when) are enumerated; every other option is
    independent, so its domain size is a pure multiplier."""
    rel: set = set()
    for c in csm.constraints:
        referenced_options(csm.formula(c["id"]), rel)
    for n, o in csm.options.items():
        if "active_when" in o:
            rel.add(n)
            referenced_options(o["active_when"], rel)
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
    g.add_argument("--config-file", help="JSON/YAML file with an object or a list of objects")
    g.add_argument("--default", action="store_true", help="validate the default configuration")
    g.add_argument("--self-test", action="store_true", help="model consistency checks")
    g.add_argument("--stats", action="store_true", help="enumerate the discretised space")
    ap.add_argument("--strict", action="store_true", help="treat soft-constraint violations as errors")
    ap.add_argument("--include-hypotheses", action="store_true", help="also enforce 'hypothesis' constraints")
    ap.add_argument("--quiet", action="store_true", help="only print the verdict per configuration")
    args = ap.parse_args(argv)

    csm = load_csm(args.csm, include_hypotheses=args.include_hypotheses)

    if args.self_test:
        problems = self_test(csm)
        print(json.dumps({"csm": str(csm.path), "csm_version": csm.version, "options": len(csm.order),
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
        from csm_model import read_model_file
        loaded = read_model_file(args.config_file)
        if isinstance(loaded, dict) and isinstance(loaded.get("configs"), list):
            loaded = loaded["configs"]          # acts_bridge.py import output
        configs = loaded if isinstance(loaded, list) else [loaded]
        configs = [c["configuration"] if isinstance(c, dict) and "configuration" in c else c for c in configs]

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
