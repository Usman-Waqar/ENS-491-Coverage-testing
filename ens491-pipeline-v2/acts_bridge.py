#!/usr/bin/env python3
"""Bridge between the CSM and NIST ACTS covering-array generation.

Subcommands
  export    CSM -> ACTS text input (<base>.acts.txt) + token map (<base>.acts.map.json)
  run       run the ACTS jar on an exported file (needs ACTS Advanced; $ACTS_JAR or --jar)
  generate  built-in constraint-aware greedy generator; writes ACTS-format CSV (fallback / cross-check)
  import    ACTS CSV output -> executable configurations (configs.json) for build_and_test.py
  coverage  independent t-way coverage check of a configuration list (verifies ACTS output,
            measures the developer suite, measures generated tests)

Policies (shared by export, generate, coverage; recorded in the map file)
  --t N                base strength (default 2)
  --soft omit|as-hard  soft constraints ignored (default) or enforced
  --inert collapse|free  collapse adds 'if not(active_when) then opt == default' (default)
  --relations none|auto  auto: one base relation over all options at strength t, plus one per
                       interaction group whose suggested_strength exceeds t (mixed strength)
  --seed none|developer  developer: the developer test variants become ACTS [Test Set] rows and
                       generation runs in extend mode

ACTS facts relied upon (ACTS User Guide, Basic 1.0 / Advanced 3.3, Nov 2023): sections [System]
[Parameter] [Relation] [Constraint] [Test Set]; types boolean/enum/int; values must not contain
, ; " \\ ; constraint operators = != > < >= <= && || => (no negation operator is documented, so
negation is pushed into the atoms); enum literals in double quotes; '--' comments; '*' don't-care;
CSV output = '#' comment lines, a header row, one row per configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import csm_model as cm  # noqa: E402
import validate_constraints as vc  # noqa: E402

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOKEN_CHARS = {";": "SEMI", "#": "HASH", "=": "EQ", ":": "COLON", "[": "LBRACK", "]": "RBRACK", "/": "SLASH",
                "!": "BANG", "%": "PCT", "*": "STAR", " ": "SPACE", ",": "COMMA", '"': "DQUOTE",
                "\\": "BSLASH", "'": "SQUOTE", "-": "DASH", ".": "DOT"}


# ==========================================================================
# Parameters and tokens
# ==========================================================================

def token_for(value: Any, index: int) -> str:
    s = str(value)
    if IDENT.match(s):
        return s
    if s == "":
        return "NONE"
    if all(ch in _TOKEN_CHARS for ch in s):
        return "_".join(_TOKEN_CHARS[ch] for ch in s)
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", s)
    return cleaned if cleaned and IDENT.match(cleaned) else f"V{index}"


class Param:
    """One ACTS parameter with its value tokens."""

    def __init__(self, csm: cm.CSM, name: str):
        o = csm.options[name]
        self.option = name
        self.name = name if IDENT.match(name) else re.sub(r"\W", "_", name)
        self.domain = o["domain_type"]
        self.binding = o["binding_time"]
        self.values = csm.settings(name)
        if self.domain == "boolean":
            self.type = "boolean"
            self.tokens = ["true" if v == 1 else "false" for v in self.values]
        elif self.domain == "integer":
            self.type = "int"
            self.tokens = [str(v) for v in self.values]
        else:
            self.type = "enum"
            toks = []
            for i, s in enumerate(o["discrete_settings"]):
                t = s.get("token") or token_for(s["value"], i)
                toks.append(t)
            if len(set(toks)) != len(toks):
                toks = [f"{t}_{i}" for i, t in enumerate(toks)]
            self.tokens = toks
        self.tok2val = dict(zip([t.lower() if self.type == "boolean" else t for t in self.tokens], self.values))
        self.val2tok = dict(zip(map(json.dumps, self.values), self.tokens))

    def tok(self, value) -> str:
        return self.val2tok[json.dumps(value)]

    def literal(self, value) -> str:
        t = self.tok(value)
        return f'"{t}"' if self.type == "enum" else t

    def decode(self, token: str):
        token = token.strip()
        if token == "*":
            return None
        key = token.lower() if self.type == "boolean" else token
        if key in self.tok2val:
            return self.tok2val[key]
        if self.type == "int":
            return int(token)
        raise ValueError(f"{self.name}: unknown value token {token!r}")


# ==========================================================================
# Generation model (constraints + policies)
# ==========================================================================

class GenModel:
    def __init__(self, csm: cm.CSM, t: int = 2, soft: str = "omit", inert: str = "collapse",
                 relations: str = "auto"):
        self.csm, self.t, self.soft, self.inert, self.relations_policy = csm, t, soft, inert, relations
        self.params = [Param(csm, n) for n in csm.order]
        self.by_option = {p.option: p for p in self.params}
        self.domains = {n: csm.settings(n) for n in csm.order}
        self.defaults = csm.defaults()
        self.rules: list[dict] = []      # {id, kind, origin, formula, text}
        for c in csm.constraints:
            if c["severity"] == "hard" or soft == "as-hard":
                self.rules.append({"id": c["id"], "kind": c["severity"], "origin": "csm",
                                   "formula": csm.formula(c["id"]), "text": c["expression"]})
        if inert == "collapse":
            for n in csm.order:
                aw = csm.options[n].get("active_when")
                if aw is None:
                    continue
                f = {"implies": [{"not": aw}, {"option": n, "op": "==", "value": self.defaults[n]}]}
                dup = next((r for r in self.rules if self._equivalent(r["formula"], f)), None)
                if dup:
                    dup.setdefault("subsumes", []).append(f"collapse:{n}")
                    continue
                self.rules.append({"id": f"collapse:{n}", "kind": "hard", "origin": "inert_collapse",
                                   "formula": f, "text": f"if not ({cm.condition_to_text(aw)}) then {n} == "
                                                         f"{json.dumps(self.defaults[n])}"})
        self.relations = self._relations()
        self._feas_cache: dict = {}
        order = sorted(csm.order, key=lambda n: -len([r for r in self.rules
                                                      if n in cm.referenced_options(r["formula"])]))
        self._search_order = order

    def _equivalent(self, a, b) -> bool:
        names = sorted(cm.referenced_options(a) | cm.referenced_options(b))
        for combo in itertools.product(*[self.domains[n] for n in names]):
            cfg = dict(self.defaults, **dict(zip(names, combo)))
            if cm.eval_condition(a, cfg) != cm.eval_condition(b, cfg):
                return False
        return True

    def _relations(self) -> list[dict]:
        rels = [{"id": "R1", "options": list(self.csm.order), "strength": self.t, "source": "base"}]
        if self.relations_policy == "auto":
            for g in self.csm.groups:
                s = int(g.get("suggested_strength", 2))
                opts = [o for o in self.csm.order if o in g["options"]]
                if s > self.t and len(opts) >= s:
                    rels.append({"id": f"R{len(rels) + 1}", "options": opts, "strength": s,
                                 "source": g["id"]})
        return rels

    # ---- validity ---------------------------------------------------------
    def valid(self, cfg: dict) -> bool:
        return all(cm.eval_condition(r["formula"], cfg) for r in self.rules)

    def partial_ok(self, partial: dict) -> bool:
        return all(cm.eval3(r["formula"], partial) is not False for r in self.rules)

    def extend(self, partial: dict) -> dict | None:
        """A full valid configuration extending `partial` (defaults preferred), or None."""
        key = frozenset((k, json.dumps(v)) for k, v in partial.items())
        if key in self._feas_cache:
            return self._feas_cache[key]
        if not self.partial_ok(partial):
            self._feas_cache[key] = None
            return None
        free = [n for n in self._search_order if n not in partial]

        def rec(i, cur):
            if i == len(free):
                return dict(cur) if self.valid(cur) else None
            n = free[i]
            dom = [self.defaults[n]] + [v for v in self.domains[n] if v != self.defaults[n]]
            for v in dom:
                cur[n] = v
                if self.partial_ok(cur):
                    r = rec(i + 1, cur)
                    if r is not None:
                        return r
                del cur[n]
            return None

        res = rec(0, dict(partial))
        self._feas_cache[key] = res
        return res

    # ---- t-way tuples -----------------------------------------------------
    def required_tuples(self) -> tuple[dict, dict]:
        """{relation id: set of feasible tuples}, {relation id: infeasible count}.
        A tuple is a tuple of (option, json(value)) pairs sorted by option order."""
        req, infeas = {}, {}
        idx = {n: i for i, n in enumerate(self.csm.order)}
        for rel in self.relations:
            s, bad = set(), 0
            for subset in itertools.combinations(sorted(rel["options"], key=idx.get), rel["strength"]):
                for combo in itertools.product(*[self.domains[n] for n in subset]):
                    tup = tuple((n, json.dumps(v)) for n, v in zip(subset, combo))
                    if tup in s:
                        continue
                    if self.extend(dict(zip(subset, combo))) is not None:
                        s.add(tup)
                    else:
                        bad += 1
            req[rel["id"]] = s
            infeas[rel["id"]] = bad
        return req, infeas

    def tuples_of(self, cfg: dict, rel: dict) -> set:
        idx = {n: i for i, n in enumerate(self.csm.order)}
        return {tuple((n, json.dumps(cfg[n])) for n in subset)
                for subset in itertools.combinations(sorted(rel["options"], key=idx.get), rel["strength"])}

    def normalise(self, cfg: dict) -> dict:
        """Complete, map to representatives, collapse inert options (if policy)."""
        full, _ = vc.complete(self.csm, cfg)
        full, _ = vc.to_representatives(self.csm, full)
        if self.inert == "collapse":
            full = vc.effective_config(self.csm, full)
        return full


# ==========================================================================
# ACTS constraint rendering
# ==========================================================================

def _fold(f):
    """Constant-fold an NNF tree (only all/any/atoms/bools)."""
    if isinstance(f, bool):
        return f
    if "all" in f or "any" in f:
        key = "all" if "all" in f else "any"
        items = [_fold(x) for x in f[key]]
        if key == "all":
            if any(x is False for x in items):
                return False
            items = [x for x in items if x is not True]
            return True if not items else (items[0] if len(items) == 1 else {"all": items})
        if any(x is True for x in items):
            return True
        items = [x for x in items if x is not False]
        return False if not items else (items[0] if len(items) == 1 else {"any": items})
    return f


def _expand_atom(gm: GenModel, atom: dict):
    """Rewrite an atom into ACTS-expressible form (may return bool / all / any)."""
    p = gm.by_option[atom["option"]]
    op = atom["op"]
    if "ref" in atom:
        q = gm.by_option[atom["ref"]]
        if p.type == "int" and q.type == "int" and op in ("==", "!=", "<", "<=", ">", ">="):
            return {"_acts": f"{p.name} {'=' if op == '==' else op} {q.name}"}
        pairs = [(a, b) for a in p.values for b in q.values
                 if cm.eval_condition(atom, {atom["option"]: a, atom["ref"]: b})]
        if not pairs:
            return False
        if len(pairs) == len(p.values) * len(q.values):
            return True
        return {"any": [{"_acts": f"({p.name} = {p.literal(a)} && {q.name} = {q.literal(b)})"} for a, b in pairs]}
    sat = [v for v in p.values if cm.eval_condition(atom, {atom["option"]: v})]
    if not sat:
        return False
    if len(sat) == len(p.values):
        return True
    if p.type == "int" and op in ("<", "<=", ">", ">=", "==", "!=") and "value" in atom:
        return {"_acts": f"{p.name} {'=' if op == '==' else op} {atom['value']}"}
    if len(sat) == 1:
        return {"_acts": f"{p.name} = {p.literal(sat[0])}"}
    unsat = [v for v in p.values if v not in sat]
    if len(unsat) == 1:
        return {"_acts": f"{p.name} != {p.literal(unsat[0])}"}
    if len(unsat) < len(sat):
        return {"all": [{"_acts": f"{p.name} != {p.literal(v)}"} for v in unsat]}
    return {"any": [{"_acts": f"{p.name} = {p.literal(v)}"} for v in sat]}


def _acts_tree(gm: GenModel, f):
    if isinstance(f, bool):
        return f
    if "all" in f or "any" in f:
        key = "all" if "all" in f else "any"
        return {key: [_acts_tree(gm, x) for x in f[key]]}
    return _expand_atom(gm, f)


def _render(t, top=True) -> str:
    if isinstance(t, bool):
        raise ValueError("boolean constant left after folding")
    if "_acts" in t:
        return t["_acts"] if top else f"({t['_acts']})"
    key = "all" if "all" in t else "any"
    sep = " && " if key == "all" else " || "
    return "(" + sep.join(_render(x, False) for x in t[key]) + ")"


def to_acts(gm: GenModel, formula) -> str | None:
    """ACTS constraint text, or None if the constraint is a tautology on the discretised domains.
    Raises ValueError if it is unsatisfiable."""
    if isinstance(formula, dict) and "implies" in formula:
        a, b = formula["implies"]
        ta = _fold(_acts_tree(gm, cm.to_nnf(a)))
        tb = _fold(_acts_tree(gm, cm.to_nnf(b)))
        if ta is False or tb is True:
            return None
        if ta is True:
            if tb is False:
                raise ValueError("constraint is unsatisfiable")
            return _render(tb)
        if tb is False:
            na = _fold(_acts_tree(gm, cm.to_nnf(a, negate=True)))
            if na is True:
                return None
            return _render(na)
        return f"({_render(ta, True)}) => ({_render(tb, True)})"
    t = _fold(_acts_tree(gm, cm.to_nnf(formula)))
    if t is True:
        return None
    if t is False:
        raise ValueError("constraint is unsatisfiable")
    return _render(t)


# ==========================================================================
# Seeds (developer suite)
# ==========================================================================

def developer_seeds(gm: GenModel) -> list[dict]:
    suite = gm.csm.data.get("test_suite", {})
    drivers = {d["id"]: d for d in suite.get("drivers", [])}
    rows, by_key = [], {}
    for v in suite.get("variants", []):
        d = drivers.get(v["driver"], {})
        cfg = dict(v["settings"])
        cfg.update(d.get("fixed_runtime", {}))
        raw, _ = vc.complete(gm.csm, cfg)
        notes = [f"{k}: {raw[k]!r} -> {gm.csm.representative(k, raw[k])!r}" for k in raw
                 if raw[k] not in gm.domains[k]]
        norm = gm.normalise(cfg)
        key = json.dumps(norm, sort_keys=True)
        if key in by_key:
            by_key[key]["variants"].append(v["id"])
            continue
        row = {"configuration": norm, "variants": [v["id"]], "mapping_notes": notes, "valid": gm.valid(norm)}
        by_key[key] = row
        rows.append(row)
    return rows


# ==========================================================================
# Export
# ==========================================================================

def export(gm: GenModel, seed: str, base: Path, title: str | None = None) -> dict:
    csm = gm.csm
    lines = [f"-- generated by acts_bridge.py export from {csm.path.name} "
             f"({csm.data['system'].get('name')} @ {str(csm.data['system'].get('commit'))[:12]})",
             f"-- policies: t={gm.t} soft={gm.soft} inert={gm.inert} relations={gm.relations_policy} seed={seed}",
             "", "[System]", f"Name: {title or csm.data['system'].get('name', 'system')}", "", "[Parameter]"]
    for p in gm.params:
        lines.append(f"{p.name} ({p.type}) : {', '.join(p.tokens)}")
    mixed = len(gm.relations) > 1
    if mixed:
        lines += ["", "[Relation]"]
        for r in gm.relations:
            names = ", ".join(gm.by_option[o].name for o in r["options"])
            lines.append(f"-- {r['source']}")
            lines.append(f"{r['id']} : ({names}, {r['strength']})")
    rendered = []
    lines += ["", "[Constraint]"]
    for r in gm.rules:
        txt = to_acts(gm, r["formula"])
        rendered.append({"id": r["id"], "origin": r["origin"], "csm": r["text"], "acts": txt,
                         "subsumes": r.get("subsumes", [])})
        if txt is None:
            lines.append(f"-- {r['id']}: tautology on the discretised domains, omitted ({r['text']})")
            continue
        extra = f" (also covers {', '.join(r['subsumes'])})" if r.get("subsumes") else ""
        lines.append(f"-- {r['id']} [{r['origin']}]{extra}: {r['text']}")
        lines.append(txt)
    seeds = developer_seeds(gm) if seed == "developer" else []
    if seeds:
        lines += ["", "[Test Set]", "-- developer test variants (tests/meson.build), mapped to partition representatives",
                  ", ".join(p.name for p in gm.params)]
        for s in seeds:
            lines.append(f"-- {', '.join(s['variants'])}" + ("" if s["valid"] else "  (VIOLATES generation constraints)"))
            lines.append(",".join(gm.by_option[o].tok(s["configuration"][o]) for o in csm.order))
    txt_path = Path(str(base) + ".acts.txt")
    map_path = Path(str(base) + ".acts.map.json")
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path.write_text("\n".join(lines) + "\n")
    meta = {
        "csm": str(csm.path), "system": csm.data["system"].get("name"), "commit": csm.data["system"].get("commit"),
        "policies": {"t": gm.t, "soft": gm.soft, "inert": gm.inert, "relations": gm.relations_policy, "seed": seed},
        "acts_flags": {"doi": -1 if mixed else gm.t, "mode": "extend" if seeds else "scratch"},
        "params": [{"param": p.name, "option": p.option, "type": p.type, "binding": p.binding,
                    "values": [{"token": t, "value": v} for t, v in zip(p.tokens, p.values)]} for p in gm.params],
        "relations": gm.relations, "constraints": rendered,
        "seeds": [{"variants": s["variants"], "configuration": s["configuration"], "notes": s["mapping_notes"],
                   "valid": s["valid"]} for s in seeds],
        "files": {"input": str(txt_path), "map": str(map_path)},
    }
    map_path.write_text(json.dumps(meta, indent=2))
    return meta


def gm_from_meta(meta: dict) -> GenModel:
    pol = meta["policies"]
    csm = cm.load_csm(meta["csm"])
    return GenModel(csm, pol["t"], pol["soft"], pol["inert"], pol["relations"])


# ==========================================================================
# Run ACTS
# ==========================================================================

def run_acts(meta: dict, jar: str | None, out_csv: Path, algo: str = "ipog", chandler: str = "forbiddentuples",
             timeout: int = 3600) -> dict:
    jar = jar or os.environ.get("ACTS_JAR")
    if not jar or not Path(jar).exists():
        raise SystemExit("ACTS jar not found: pass --jar or set $ACTS_JAR (ACTS Advanced is obtained from NIST "
                         "by e-mail request; see docs/phase1_2_design.md). Use 'generate --engine builtin' meanwhile.")
    f = meta["acts_flags"]
    cmd = ["java", f"-Ddoi={f['doi']}", f"-Dalgo={algo}", f"-Dmode={f['mode']}", f"-Dchandler={chandler}",
           "-Doutput=csv", "-Dcheck=on", "-Drandstar=off", "-Dprogress=off",
           "-jar", jar, meta["files"]["input"], str(out_csv)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    rec = {"command": " ".join(cmd), "returncode": r.returncode, "stdout_tail": r.stdout[-2000:],
           "stderr_tail": r.stderr[-2000:], "output": str(out_csv)}
    Path(str(out_csv) + ".run.json").write_text(json.dumps(rec, indent=2))
    if r.returncode != 0 or not out_csv.exists():
        raise SystemExit(f"ACTS failed (exit {r.returncode}); see {out_csv}.run.json")
    return rec


# ==========================================================================
# Built-in generator (writes ACTS-format CSV)
# ==========================================================================

def generate_builtin(gm: GenModel, seeds: list[dict], rng_seed: int = 0, candidates: int = 25,
                     max_rows: int = 2000) -> tuple[list[dict], dict]:
    rng = random.Random(rng_seed)
    req, infeas = gm.required_tuples()
    rel_by_id = {r["id"]: r for r in gm.relations}
    uncovered = set()
    for rid, s in req.items():
        uncovered |= {(rid, t) for t in s}
    total = len(uncovered)
    rows = []
    for s in seeds:
        if gm.valid(s["configuration"]):
            for rel in gm.relations:
                uncovered -= {(rel["id"], t) for t in gm.tuples_of(s["configuration"], rel)}
    seed_covered = total - len(uncovered)
    # index: option -> list of (rid, subset) containing it
    idx = {n: i for i, n in enumerate(gm.csm.order)}
    subsets_with = {n: [] for n in gm.csm.order}
    for rel in gm.relations:
        for subset in itertools.combinations(sorted(rel["options"], key=idx.get), rel["strength"]):
            for n in subset:
                subsets_with[n].append((rel["id"], subset))

    def gain(partial, n, v):
        g = 0
        trial = dict(partial)
        trial[n] = v
        for rid, subset in subsets_with[n]:
            if all(x in trial for x in subset):
                if (rid, tuple((x, json.dumps(trial[x])) for x in subset)) in uncovered:
                    g += 1
        return g

    ordered = sorted(uncovered)
    while uncovered and len(rows) < max_rows:
        best, best_gain = None, -1
        pool = [t for t in ordered if t in uncovered]
        for _ in range(candidates):
            rid, tup = rng.choice(pool[: max(1, min(len(pool), 50))]) if rng.random() < 0.5 else rng.choice(pool)
            partial = {n: json.loads(v) for n, v in tup}
            if gm.extend(partial) is None:
                continue
            free = [n for n in gm.csm.order if n not in partial]
            rng.shuffle(free)
            ok = True
            for n in free:
                opts = []
                for v in gm.domains[n]:
                    trial = dict(partial, **{n: v})
                    if not gm.partial_ok(trial):
                        continue
                    opts.append((gain(partial, n, v), v == gm.defaults[n], rng.random(), v))
                if not opts:
                    ok = False
                    break
                opts.sort(reverse=True)
                partial[n] = opts[0][3]
            if not ok or not gm.valid(partial):
                fixed = gm.extend({n: json.loads(v) for n, v in tup})
                if fixed is None:
                    continue
                partial = fixed
            covered = set()
            for rel in gm.relations:
                covered |= {(rel["id"], t) for t in gm.tuples_of(partial, rel)}
            gnew = len(covered & uncovered)
            if gnew > best_gain:
                best, best_gain = partial, gnew
        if best is None or best_gain <= 0:
            break
        rows.append(best)
        for rel in gm.relations:
            uncovered -= {(rel["id"], t) for t in gm.tuples_of(best, rel)}
    stats = {"required": total, "infeasible": sum(infeas.values()), "covered_by_seeds": seed_covered,
             "rows_generated": len(rows), "uncovered_left": len(uncovered),
             "relations": {r["id"]: {"strength": r["strength"], "options": len(r["options"]),
                                     "required": len(req[r["id"]])} for r in gm.relations}}
    return rows, stats


def generate_two_level(gm: GenModel, seeds: list[dict], rng_seed: int = 0, candidates: int = 25) -> tuple[list[dict], dict]:
    """Cost-aware generation (after Demiroz & Yilmaz, cost-aware CIT): rebuilds are the expensive step,
    so (A) cover every required tuple that involves only compile-time options with as few rows as the
    greedy finds (runtime options at defaults), then (B) cover the remaining tuples, which all involve a
    runtime option, by adding rows that REUSE an existing compile-time configuration and only choose
    runtime values. Phase B rows never cost a rebuild."""
    rng = random.Random(rng_seed)
    ct = [n for n in gm.csm.order if gm.csm.options[n]["binding_time"] == "compile_time"]
    rt = [n for n in gm.csm.order if n not in ct]
    req, infeas = gm.required_tuples()
    uncovered = set()
    for rid, s in req.items():
        uncovered |= {(rid, t) for t in s}
    total = len(uncovered)
    rows = []

    def cover(row):
        for rel in gm.relations:
            uncovered.difference_update({(rel["id"], t) for t in gm.tuples_of(row, rel)})

    for s in seeds:
        if gm.valid(s["configuration"]):
            cover(s["configuration"])
    seed_covered = total - len(uncovered)
    ct_set = set(ct)
    idx = {n: i for i, n in enumerate(gm.csm.order)}
    subsets_with = {n: [] for n in gm.csm.order}
    for rel in gm.relations:
        for subset in itertools.combinations(sorted(rel["options"], key=idx.get), rel["strength"]):
            for n in subset:
                subsets_with[n].append((rel["id"], subset))

    def gain(partial, n, v, only_ct=False):
        g = 0
        trial = dict(partial, **{n: v})
        for rid, subset in subsets_with[n]:
            if only_ct and not set(subset) <= ct_set:
                continue
            if all(x in trial for x in subset) and \
                    (rid, tuple((x, json.dumps(trial[x])) for x in subset)) in uncovered:
                g += 1
        return g

    # ---- phase A: compile-time tuples ----
    def ct_uncovered():
        return sorted(t for t in uncovered if all(n in ct_set for n, _ in t[1]))

    phase_a = 0
    while True:
        pool = ct_uncovered()
        if not pool:
            break
        best, best_gain = None, 0
        for _ in range(candidates):
            rid, tup = rng.choice(pool)
            partial = {n: json.loads(v) for n, v in tup}
            for n in rt:
                partial[n] = gm.defaults[n]
            if gm.extend(partial) is None:
                continue
            free = [n for n in ct if n not in partial]
            rng.shuffle(free)
            ok = True
            for n in free:
                opts = [(gain(partial, n, v, True), v == gm.defaults[n], rng.random(), v)
                        for v in gm.domains[n] if gm.partial_ok(dict(partial, **{n: v}))]
                if not opts:
                    ok = False
                    break
                partial[n] = max(opts)[3]
            if not ok or not gm.valid(partial):
                continue
            g = sum(1 for rel in gm.relations for t in gm.tuples_of(partial, rel) if (rel["id"], t) in uncovered)
            if g > best_gain:
                best, best_gain = partial, g
        if best is None:
            break
        rows.append(best)
        cover(best)
        phase_a += 1

    # ---- phase B: runtime tuples on existing builds ----
    keys = []
    for r in [s["configuration"] for s in seeds] + rows:
        k = {n: r[n] for n in ct}
        if k not in keys:
            keys.append(k)
    phase_b = 0
    while uncovered:
        best, best_gain = None, 0
        for k in keys:
            partial = dict(k)
            order = list(rt)
            rng.shuffle(order)
            for n in order:
                opts = [(gain(partial, n, v), v == gm.defaults[n], rng.random(), v)
                        for v in gm.domains[n] if gm.partial_ok(dict(partial, **{n: v}))]
                if not opts:
                    partial = None
                    break
                partial[n] = max(opts)[3]
            if partial is None or not gm.valid(partial):
                continue
            g = sum(1 for rel in gm.relations for t in gm.tuples_of(partial, rel) if (rel["id"], t) in uncovered)
            if g > best_gain:
                best, best_gain = partial, g
        if best is None:
            break
        rows.append(best)
        cover(best)
        phase_b += 1
    builds = {json.dumps({n: r[n] for n in ct}, sort_keys=True)
              for r in [s["configuration"] for s in seeds] + rows}
    stats = {"strategy": "two-level (cost-aware)", "required": total, "infeasible": sum(infeas.values()),
             "covered_by_seeds": seed_covered, "rows_generated": len(rows), "rows_phase_a_new_builds": phase_a,
             "rows_phase_b_reused_builds": phase_b, "distinct_builds_total": len(builds),
             "uncovered_left": len(uncovered)}
    return rows, stats


def write_csv(gm: GenModel, rows: list[dict], path: Path, header_comments: list[str]) -> None:
    lines = [f"# {c}" for c in header_comments]
    lines.append(",".join(p.name for p in gm.params))
    for r in rows:
        lines.append(",".join(gm.by_option[o].tok(r[o]) if r.get(o) is not None else "*" for o in gm.csm.order))
    path.write_text("\n".join(lines) + "\n")


# ==========================================================================
# Import
# ==========================================================================

def parse_csv(text: str) -> tuple[list[str], list[list[str]], list[str]]:
    comments, header, rows = [], None, []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        if ln.lstrip().startswith("#"):
            comments.append(ln.strip())
            continue
        cells = [c.strip() for c in ln.split(",")]
        if header is None:
            header = cells
        else:
            rows.append(cells)
    if header is None:
        raise ValueError("no header row in ACTS CSV output")
    return header, rows, comments


def group_key(csm: cm.CSM, eff: dict) -> str:
    ct = {n: eff[n] for n in csm.compile_time_options()}
    return hashlib.sha1(json.dumps(ct, sort_keys=True).encode()).hexdigest()[:10]


def import_csv(meta: dict, csv_text: str, fill: str = "cost-aware", source: str = "acts",
               provenance: dict | None = None) -> dict:
    gm = gm_from_meta(meta)
    header, rows, comments = parse_csv(csv_text)
    expected = [p["param"] for p in meta["params"]]
    if header != expected:
        raise SystemExit(f"header mismatch:\n  got      {header}\n  expected {expected}")
    params = [gm.by_option[p["option"]] for p in meta["params"]]
    seeds = {json.dumps(s["configuration"], sort_keys=True): s for s in meta.get("seeds", [])}
    ct = set(gm.csm.compile_time_options())
    configs, problems, keys_in_use = [], [], []
    for i, cells in enumerate(rows, 1):
        if len(cells) != len(params):
            problems.append(f"row {i}: {len(cells)} cells, expected {len(params)}")
            continue
        partial, dont_care = {}, []
        for p, c in zip(params, cells):
            v = p.decode(c)
            if v is None:
                dont_care.append(p.option)
            else:
                partial[p.option] = v
        cfg = None
        if dont_care:
            if fill == "cost-aware":
                # runtime don't-cares -> default; compile-time -> reuse a compile key already in use
                base = dict(partial)
                for n in dont_care:
                    if n not in ct:
                        base[n] = gm.defaults[n]
                for prev in keys_in_use:
                    trial = dict(base)
                    for n in dont_care:
                        if n in ct:
                            trial[n] = prev[n]
                    if gm.valid(trial):
                        cfg = trial
                        break
            if cfg is None:
                trial = dict(partial)
                for n in dont_care:
                    trial[n] = gm.defaults[n]
                cfg = trial if gm.valid(trial) else gm.extend(partial)
        else:
            cfg = partial
        if cfg is None or not gm.valid(cfg):
            problems.append(f"row {i}: violates generation constraints {cells}")
            continue
        rep = vc.validate(gm.csm, cfg, strict=(gm.soft == "as-hard"))
        if not rep["valid"]:
            problems.append(f"row {i}: invalid per CSM: {rep['errors']}")
            continue
        eff = rep["effective_configuration"]
        keys_in_use.append({n: eff[n] for n in ct})
        key = json.dumps(gm.normalise(cfg), sort_keys=True)
        seed = seeds.get(key)
        configs.append({
            "config_id": None,
            "source": "seed" if seed else source,
            "generator": {**(provenance or {}), "row": i},
            "seed_variants": seed["variants"] if seed else [],
            "filled_dont_cares": dont_care,
            "compile_group": group_key(gm.csm, eff),
            "soft_violations": [v["id"] for v in rep["violated_constraints"]],
            "configuration": cfg,
        })
    order = {}
    for c in configs:
        order.setdefault(c["compile_group"], len(order))
    configs.sort(key=lambda c: (order[c["compile_group"]], c["generator"]["row"]))
    prefix = f"{source}-t{gm.t}"
    for k, c in enumerate(configs, 1):
        c["config_id"] = f"{prefix}-{k:03d}"
    return {"configs": configs, "problems": problems, "acts_comments": comments,
            "builds": len(order), "rows": len(rows), "policies": meta["policies"]}


# ==========================================================================
# Coverage
# ==========================================================================

def coverage(gm: GenModel, configs: list[dict], max_list: int = 50) -> dict:
    req, infeas = gm.required_tuples()
    rep = {"policies": {"t": gm.t, "soft": gm.soft, "inert": gm.inert, "relations": gm.relations_policy},
           "configurations": len(configs), "relations": {}, "invalid_configurations": []}
    covered_by = {r["id"]: set() for r in gm.relations}
    for i, c in enumerate(configs):
        norm = gm.normalise(c)
        if not gm.valid(norm):
            rep["invalid_configurations"].append(i)
        for rel in gm.relations:
            covered_by[rel["id"]] |= gm.tuples_of(norm, rel)
    tot_req = tot_cov = 0
    for rel in gm.relations:
        r = req[rel["id"]]
        cov = r & covered_by[rel["id"]]
        miss = sorted(r - cov)
        tot_req += len(r)
        tot_cov += len(cov)
        rep["relations"][rel["id"]] = {
            "source": rel["source"], "strength": rel["strength"], "options": rel["options"],
            "required": len(r), "infeasible_excluded": infeas[rel["id"]], "covered": len(cov),
            "percent": round(100.0 * len(cov) / len(r), 2) if r else 100.0,
            "uncovered_sample": [{n: json.loads(v) for n, v in t} for t in miss[:max_list]],
            "uncovered_count": len(miss)}
    rep["total"] = {"required": tot_req, "covered": tot_cov,
                    "percent": round(100.0 * tot_cov / tot_req, 2) if tot_req else 100.0}
    return rep


def load_configs(path: str) -> list[dict]:
    data = cm.read_model_file(path)
    if isinstance(data, dict) and "configs" in data:
        data = data["configs"]
    data = data if isinstance(data, list) else [data]
    return [d["configuration"] if isinstance(d, dict) and "configuration" in d else d for d in data]


# ==========================================================================
# CLI
# ==========================================================================

def _gm_args(p):
    p.add_argument("--csm", default=str(HERE / "csm.json"))
    p.add_argument("--t", type=int, default=2)
    p.add_argument("--soft", choices=["omit", "as-hard"], default="omit")
    p.add_argument("--inert", choices=["collapse", "free"], default="collapse")
    p.add_argument("--relations", choices=["none", "auto"], default="auto")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("export")
    _gm_args(pe)
    pe.add_argument("--seed", choices=["none", "developer"], default="developer")
    pe.add_argument("-o", "--out-base", required=True, help="path prefix, e.g. acts/inih_t2")
    pr = sub.add_parser("run")
    pr.add_argument("--map", required=True)
    pr.add_argument("--jar")
    pr.add_argument("--algo", default="ipog")
    pr.add_argument("--chandler", default="forbiddentuples")
    pr.add_argument("-o", "--output", required=True)
    pg = sub.add_parser("generate")
    pg.add_argument("--map", required=True)
    pg.add_argument("--engine", choices=["builtin"], default="builtin")
    pg.add_argument("--rng-seed", type=int, default=0)
    pg.add_argument("--candidates", type=int, default=25)
    pg.add_argument("--strategy", choices=["two-level", "one-level"], default="two-level",
                    help="two-level: cost-aware (compile-time tuples first, runtime tuples on existing builds)")
    pg.add_argument("-o", "--output", required=True)
    pi = sub.add_parser("import")
    pi.add_argument("--map", required=True)
    pi.add_argument("--csv", required=True)
    pi.add_argument("--fill", choices=["cost-aware", "default"], default="cost-aware")
    pi.add_argument("--source", default=None, help="label for provenance (acts|builtin); read from the CSV if omitted")
    pi.add_argument("-o", "--output", required=True)
    pc = sub.add_parser("coverage")
    _gm_args(pc)
    pc.add_argument("--map", help="take the policies from an export map instead of flags")
    pc.add_argument("configs", help="configs.json (import output), a list of configs, or 'developer'")
    pc.add_argument("-o", "--output")
    a = ap.parse_args(argv)

    if a.cmd == "export":
        gm = GenModel(cm.load_csm(a.csm), a.t, a.soft, a.inert, a.relations)
        meta = export(gm, a.seed, Path(a.out_base))
        print(json.dumps({"input": meta["files"]["input"], "map": meta["files"]["map"],
                          "parameters": len(meta["params"]), "constraints": sum(1 for c in meta["constraints"] if c["acts"]),
                          "relations": len(meta["relations"]), "seeds": len(meta["seeds"]),
                          "acts_flags": meta["acts_flags"]}, indent=2))
        return 0

    if a.cmd == "run":
        meta = json.loads(Path(a.map).read_text())
        rec = run_acts(meta, a.jar, Path(a.output), a.algo, a.chandler)
        print(json.dumps(rec, indent=2))
        return 0

    if a.cmd == "generate":
        meta = json.loads(Path(a.map).read_text())
        gm = gm_from_meta(meta)
        seeds = [{"configuration": s["configuration"]} for s in meta.get("seeds", [])]
        if a.strategy == "two-level":
            rows, stats = generate_two_level(gm, seeds, a.rng_seed, a.candidates)
        else:
            rows, stats = generate_builtin(gm, seeds, a.rng_seed, a.candidates)
            stats["strategy"] = "one-level"
        all_rows = [s["configuration"] for s in seeds] + rows
        write_csv(gm, all_rows, Path(a.output), [
            f"Degree of interaction coverage: {meta['acts_flags']['doi']}",
            f"Number of parameters: {len(gm.params)}",
            f"Maximum number of values per parameter: {max(len(p.values) for p in gm.params)}",
            f"Number of configurations: {len(all_rows)}",
            f"Generator: acts_bridge builtin greedy, {a.strategy} (rng_seed={a.rng_seed}, candidates={a.candidates}); "
            f"mode={meta['acts_flags']['mode']}; seeds={len(seeds)}"])
        Path(a.output + ".stats.json").write_text(json.dumps(stats, indent=2))
        print(json.dumps(stats, indent=2))
        return 0 if stats["uncovered_left"] == 0 else 1

    if a.cmd == "import":
        meta = json.loads(Path(a.map).read_text())
        text = Path(a.csv).read_text()
        source = a.source or ("builtin" if "acts_bridge builtin" in text else "acts")
        res = import_csv(meta, text, a.fill, source, {"engine": source, "csv": a.csv})
        Path(a.output).write_text(json.dumps(res, indent=2))
        print(json.dumps({"rows": res["rows"], "configs": len(res["configs"]), "builds": res["builds"],
                          "seed_rows": sum(c["source"] == "seed" for c in res["configs"]),
                          "problems": res["problems"]}, indent=2))
        return 1 if res["problems"] else 0

    if a.cmd == "coverage":
        if a.map:
            gm = gm_from_meta(json.loads(Path(a.map).read_text()))
        else:
            gm = GenModel(cm.load_csm(a.csm), a.t, a.soft, a.inert, a.relations)
        if a.configs == "developer":
            configs = [s["configuration"] for s in developer_seeds(gm)]
        else:
            configs = load_configs(a.configs)
        rep = coverage(gm, configs)
        text = json.dumps(rep, indent=2)
        if a.output:
            Path(a.output).write_text(text)
        print(json.dumps({"configurations": rep["configurations"], "total": rep["total"],
                          "by_relation": {k: f"{v['covered']}/{v['required']} ({v['percent']}%) t={v['strength']} "
                                             f"{v['source']}" for k, v in rep["relations"].items()},
                          "invalid_configurations": rep["invalid_configurations"]}, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
