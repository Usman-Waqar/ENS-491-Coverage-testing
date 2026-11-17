#!/usr/bin/env python3
"""S7 - score a discovered CSM against a reference CSM (RQ1).

  compare.py --reference csm.yaml --discovered csm_discovered.yaml \
             [--aliases discover/reference_aliases.yaml] [-o report.json] [--markdown report.md]

Metrics
  options       precision / recall by name (plus an explicit, human-maintained
                alias map for differently named elements, e.g. runtime options)
  per option    binding, domain type, default, active_when (semantic
                equivalence: same truth value on every combination of the
                reference settings of the options involved), settings recall,
                integer range minimum, partition intervals
  constraints   semantic: a reference constraint is recovered if some
                discovered constraint is equivalent to it on the discretised
                subspace; a discovered constraint is correct if every
                configuration valid under the reference constraints satisfies it
  valid space   Jaccard index of the valid configuration sets (hard constraints)
                over the subspace of options mentioned by any constraint
  interactions  precision / recall of option pairs implied by interaction groups
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import csm_model as cm  # noqa: E402


def _rename(cond, mapping: dict):
    if isinstance(cond, bool):
        return cond
    if isinstance(cond, dict):
        out = {}
        for k, v in cond.items():
            if k in ("option", "ref"):
                out[k] = mapping.get(v, v)
            elif isinstance(v, list):
                out[k] = [_rename(x, mapping) if isinstance(x, dict) else x for x in v]
            elif isinstance(v, dict):
                out[k] = _rename(v, mapping)
            else:
                out[k] = v
        return out
    return cond


def _pr(tp: int, n_disc: int, n_ref: int) -> dict:
    p = tp / n_disc if n_disc else None
    r = tp / n_ref if n_ref else None
    f = (2 * p * r / (p + r)) if p and r else (0.0 if p is not None and r is not None else None)
    return {"tp": tp, "discovered": n_disc, "reference": n_ref,
            "precision": round(p, 3) if p is not None else None,
            "recall": round(r, 3) if r is not None else None,
            "f1": round(f, 3) if f is not None else None}


def equivalent(a, b, ref, names: list[str]) -> bool:
    base = ref.defaults()
    for combo in itertools.product(*[ref.settings(n) for n in names]):
        cfg = dict(base, **dict(zip(names, combo)))
        try:
            va = cm.eval_condition(a, cfg) if a is not None else True
            vb = cm.eval_condition(b, cfg) if b is not None else True
        except KeyError:
            return False
        if va != vb:
            return False
    return True


def compare(ref: cm.CSM, disc: cm.CSM, aliases: dict) -> dict:
    to_ref = {d: aliases.get(d, d) for d in disc.order}
    matched = {d: r for d, r in to_ref.items() if r in ref.options}
    rep = {"options": _pr(len(matched), len(disc.order), len(ref.order)),
           "unmatched_discovered": [d for d in disc.order if d not in matched],
           "missed_reference": [r for r in ref.order if r not in matched.values()],
           "per_option": {}}

    field_hits = {"binding": 0, "type": 0, "default": 0, "active_when": 0}
    settings_recall = []
    for d, r in matched.items():
        do, ro = disc.options[d], ref.options[r]
        row = {"discovered_as": d}
        row["binding"] = do["binding_time"] == ro["binding_time"]
        row["type"] = do["domain_type"] == ro["domain_type"]
        row["default"] = do["default_value"] == ro["default_value"]
        da = _rename(do.get("active_when"), to_ref) if do.get("active_when") is not None else None
        ra = ro.get("active_when")
        names = sorted((cm.referenced_options(da) if da is not None else set()) |
                       (cm.referenced_options(ra) if ra is not None else set()))
        names = [n for n in names if n in ref.options]
        row["active_when"] = equivalent(da, ra, ref, names)
        row["active_when_text"] = {"reference": cm.condition_to_text(ra) if ra is not None else None,
                                   "discovered": cm.condition_to_text(da) if da is not None else None}
        rs, ds = ref.settings(r), disc.settings(d)
        hit = sum(1 for v in rs if v in ds)
        row["settings_recall"] = round(hit / len(rs), 3)
        row["settings"] = {"reference": rs, "discovered": ds}
        if ro["domain_type"] == "integer":
            row["range_min"] = {"reference": ro.get("valid_range", {}).get("min"),
                                "discovered": do.get("valid_range", {}).get("min")}
            row["range_min_match"] = row["range_min"]["reference"] == row["range_min"]["discovered"]
            rint = [s.get("interval") for s in ro["discrete_settings"]]
            dint = [s.get("interval") for s in do["discrete_settings"]]
            row["partition_intervals_equal"] = sorted(map(tuple, filter(None, rint))) == \
                sorted(map(tuple, filter(None, dint)))
        settings_recall.append(row["settings_recall"])
        for k in field_hits:
            field_hits[k] += bool(row[k])
        rep["per_option"][r] = row
    n = len(matched) or 1
    rep["field_accuracy"] = {k: round(v / n, 3) for k, v in field_hits.items()}
    rep["settings_recall_mean"] = round(sum(settings_recall) / n, 3)
    ints = [row for row in rep["per_option"].values() if "range_min_match" in row]
    if ints:
        rep["integer_range_min_accuracy"] = round(sum(r["range_min_match"] for r in ints) / len(ints), 3)
        rep["integer_partition_agreement"] = round(sum(r["partition_intervals_equal"] for r in ints) / len(ints), 3)

    # ---------------- constraints ----------------
    ref_cons = [(c["id"], c["severity"], ref.formula(c["id"])) for c in ref.constraints]

    def score_constraints(pool):
        disc_cons = []
        for c in pool:
            f = _rename(disc.formula(c["id"]), to_ref)
            if cm.referenced_options(f) <= set(ref.options):
                disc_cons.append((c["id"], c["severity"], f))
        recovered, correct = [], []
        for rid, rk, rf in ref_cons:
            for did, dk, df in disc_cons:
                names = sorted(cm.referenced_options(rf) | cm.referenced_options(df))
                if equivalent(rf, df, ref, names):
                    recovered.append({"reference": rid, "discovered": did, "kind_match": rk == dk})
                    break
        ref_hard = [f for _, k, f in ref_cons if k == "hard"]
        for did, dk, df in disc_cons:
            names = sorted(set().union(*[cm.referenced_options(f) for f in ref_hard + [df]]))
            names = [x for x in names if x in ref.options]
            base = ref.defaults()
            ok = True
            for combo in itertools.product(*[ref.settings(x) for x in names]):
                cfg = dict(base, **dict(zip(names, combo)))
                if all(cm.eval_condition(f, cfg) for f in ref_hard) and not cm.eval_condition(df, cfg):
                    ok = dk == "soft"  # a soft constraint may exclude valid configurations by design
                    if not ok:
                        break
            if ok:
                correct.append(did)
        return disc_cons, {"recall": _pr(len(recovered), len(disc_cons), len(ref_cons))["recall"],
                           "precision": round(len(correct) / len(disc_cons), 3) if disc_cons else None,
                           "recovered": recovered, "correct_discovered": correct,
                           "reference_ids": [c[0] for c in ref_cons],
                           "discovered_ids": [c[0] for c in disc_cons]}

    disc_cons, rep["constraints"] = score_constraints(disc.constraints)
    pool_h = [c for c in disc.all_constraints if c.get("status", "confirmed") != "refuted"]
    _, rep["constraints_incl_hypotheses"] = score_constraints(pool_h)

    # ---------------- valid space ----------------
    names = sorted(set().union(set(), *[cm.referenced_options(f) for _, k, f in ref_cons + disc_cons if k == "hard"]))
    names = [x for x in names if x in ref.options]
    inter = union = 0
    base = ref.defaults()
    for combo in itertools.product(*[ref.settings(x) for x in names]) if names else [()]:
        cfg = dict(base, **dict(zip(names, combo)))
        a = all(cm.eval_condition(f, cfg) for _, k, f in ref_cons if k == "hard")
        b = all(cm.eval_condition(f, cfg) for _, k, f in disc_cons if k == "hard")
        inter += a and b
        union += a or b
    rep["valid_space_jaccard"] = {"options": names, "jaccard": round(inter / union, 3) if union else None}

    # ---------------- interactions ----------------
    def pairs(groups, mapping):
        out = set()
        for g in groups:
            opts = sorted({mapping.get(o, o) for o in g["options"]})
            out |= set(itertools.combinations(opts, 2))
        return out
    rp, dp = pairs(ref.groups, {}), pairs(disc.groups, to_ref)
    rep["interaction_pairs"] = _pr(len(rp & dp), len(dp), len(rp))
    rep["interaction_pairs"]["missed"] = sorted(map(list, rp - dp))
    rep["interaction_pairs"]["extra"] = sorted(map(list, dp - rp))
    return rep


def to_markdown(rep: dict, title: str) -> str:
    o = rep["options"]
    lines = [f"# {title}", "",
             "| Element | Precision | Recall | Notes |", "|---|---|---|---|",
             f"| Options | {o['precision']} | {o['recall']} | {o['tp']} matched of {o['reference']} reference, "
             f"{o['discovered']} discovered |",
             f"| Constraints (confirmed) | {rep['constraints']['precision']} | {rep['constraints']['recall']} | "
             f"recovered {[r['reference'] for r in rep['constraints']['recovered']]} |",
             f"| Constraints (incl. hypotheses) | {rep['constraints_incl_hypotheses']['precision']} | "
             f"{rep['constraints_incl_hypotheses']['recall']} | "
             f"recovered {[r['reference'] for r in rep['constraints_incl_hypotheses']['recovered']]} |",
             f"| Interaction pairs | {rep['interaction_pairs']['precision']} | {rep['interaction_pairs']['recall']} | "
             f"{rep['interaction_pairs']['tp']} of {rep['interaction_pairs']['reference']} reference pairs |",
             "", "| Field accuracy (matched options) | Value |", "|---|---|"]
    for k, v in rep["field_accuracy"].items():
        lines.append(f"| {k} | {v} |")
    lines.append(f"| settings recall (mean) | {rep['settings_recall_mean']} |")
    if "integer_range_min_accuracy" in rep:
        lines.append(f"| integer range minimum | {rep['integer_range_min_accuracy']} |")
        lines.append(f"| integer partitions identical | {rep['integer_partition_agreement']} |")
    lines.append(f"| valid-space Jaccard | {rep['valid_space_jaccard']['jaccard']} |")
    lines += ["", "| Option | binding | type | default | active_when | settings recall |", "|---|---|---|---|---|---|"]
    for n, r in rep["per_option"].items():
        mark = lambda b: "yes" if b else "**no**"  # noqa: E731
        lines.append(f"| {n} | {mark(r['binding'])} | {mark(r['type'])} | {mark(r['default'])} | "
                     f"{mark(r['active_when'])} | {r['settings_recall']} |")
    if rep["unmatched_discovered"] or rep["missed_reference"]:
        lines += ["", f"Unmatched discovered: {rep['unmatched_discovered']}",
                  f"Missed reference: {rep['missed_reference']}"]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--reference", required=True)
    ap.add_argument("--discovered", required=True)
    ap.add_argument("--aliases")
    ap.add_argument("-o", "--output")
    ap.add_argument("--markdown")
    ap.add_argument("--title", default="Discovered model vs reference")
    a = ap.parse_args(argv)
    ref = cm.load_csm(a.reference)
    disc = cm.load_csm(a.discovered)
    aliases = cm.read_model_file(a.aliases) if a.aliases else {}
    rep = compare(ref, disc, aliases.get("options", aliases) if isinstance(aliases, dict) else {})
    text = json.dumps(rep, indent=2, default=list)
    if a.output:
        Path(a.output).write_text(text)
    if a.markdown:
        Path(a.markdown).write_text(to_markdown(rep, a.title))
    if not a.output:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
