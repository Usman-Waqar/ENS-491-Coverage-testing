#!/usr/bin/env python3
"""S6 - merge discovery stage outputs into a CSM v2 model.

  merge.py view  --s1 s1.json --s2 s2.json --s3 s3.json [--s4 s4.json] -o view.json
      a light 'model view' that verify.py (S5) can check
  merge.py model --s1 ... --s2 ... --s3 ... [--s4 ...] --s5 s5.json
                 [--harness csm.yaml] -o csm_discovered.yaml
      the full v2 model, with statuses from S5 and provenance per element

Heuristics used where no stage provides the information are labelled as such
in the model (rationale fields), so RQ1 scoring can tell static facts from
filled-in guesses.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import csm_model as cm  # noqa: E402
import csm_tool  # noqa: E402

INT_MAX = 2147483647


def _load(p):
    return json.loads(Path(p).read_text()) if p else None


def _string_values(default: str, choices) -> list:
    vals = [default]
    if choices:
        vals += [c for c in choices if c not in vals]
    if len(default) > 1:
        vals += [ch for ch in dict.fromkeys(default) if ch not in vals]
    if "" not in vals:
        vals.append("")
    return vals


def _int_partitions(default: int, lo: int, hi: int) -> list:
    parts = []
    if lo < default:
        parts.append({"label": "min_boundary", "interval": [lo, lo], "representative": lo,
                      "rationale": "heuristic: smallest legal value (S5 bound probe)"})
        if lo + 1 <= default - 1:
            rep = max(lo + 1, default // 10)
            parts.append({"label": "below_default", "interval": [lo + 1, default - 1], "representative": rep,
                          "rationale": "heuristic: representative ~default/10"})
    parts.append({"label": "default", "interval": [max(lo, default), default], "representative": default,
                  "rationale": "default value from the source"})
    if default < hi:
        parts.append({"label": "above_default", "interval": [default + 1, "max"],
                      "representative": min(hi, default * 20),
                      "rationale": "heuristic: representative 20x default"})
    if parts[0]["interval"][0] != lo:
        parts[0]["interval"][0] = lo
    return parts


def build_view(s1: dict, s2: dict | None, s3: dict | None, s4: dict | None) -> dict:
    view = {"options": {}, "constraints": [], "groups": []}
    for o in s1["options"]:
        if o.get("dead"):
            continue
        view["options"][o["name"]] = {"type": o["type"], "default": o["default"], "binding": "compile_time",
                                      "active_when": o.get("active_when"), "settings": None}
    for c in (s3 or {}).get("candidates", []):
        if c.get("values"):
            view["options"][c["name"]] = {"type": c["type"], "default": c.get("default", c["values"][0]),
                                          "binding": "runtime", "active_when": None, "settings": c["values"]}
    for i, c in enumerate(s1.get("constraint_candidates", [])):
        if c["options"]:
            view["constraints"].append({"id": f"S1_E{i + 1}", "kind": "hard",
                                        "expr": "false", "note": c["message"]})
    if s4:
        for o in s4.get("options", []):
            if o["name"] in view["options"]:
                view["options"][o["name"]].update({k: v for k, v in o.items() if k in ("settings",)})
            else:
                view["options"][o["name"]] = {"type": o["type"], "default": o["default"],
                                              "binding": o.get("binding", "runtime"),
                                              "active_when": o.get("active_when"), "settings": o.get("settings")}
        for c in s4.get("constraints", []):
            view["constraints"].append({"id": c["id"], "kind": c["kind"], "expr": c["expr"]})
    return view


def build_model(s1: dict, s2: dict | None, s3: dict | None, s4: dict | None, s5: dict | None,
                harness: dict | None) -> dict:
    meson = (s2 or {}).get("meson") or {}
    aliases = meson.get("aliases", {})
    docs = (s2 or {}).get("docs", {})
    inert = (s5 or {}).get("inertness", {})
    bounds = (s5 or {}).get("bounds", {})
    options = []
    for o in s1["options"]:
        if o.get("dead"):
            continue
        n = o["name"]
        al = aliases.get(n, {})
        stages = ["S1_cpp"] + (["S2_build"] if al or docs.get(n) else [])
        confirmed = []
        desc = o.get("doc") or (meson.get("options", {}).get(al.get("meson_option"), {}) or {}).get("description") \
            or f"Configuration macro {n}."
        dom: dict = {"type": o["type"]}
        if o["type"] == "boolean":
            dom["values"] = [o["default"], 1 - o["default"]]
        elif o["type"] == "string":
            vals = _string_values(o["default"], al.get("meson_choices"))
            dom["values"] = [{"value": v, "token": csm_tool.derive_token(v, i)} for i, v in enumerate(vals)]
            dom["rationale"] = ("heuristic: default, build-system choices, each character of the default "
                                "as a singleton set, and the empty set")
        else:
            b = bounds.get(n, {})
            lo = b.get("min_clean")
            lo = lo if lo is not None else (al.get("meson_min") if al.get("meson_min") is not None else 0)
            hi = al.get("meson_max") if al.get("meson_max") is not None else INT_MAX
            dom["range"] = {"min": lo, "max": hi}
            if b.get("status") == "confirmed":
                dom["range"]["evidence"] = "S5 sanitizer probe: " + ", ".join(
                    f"{p['value']}={p['outcome']}" for p in b["probes"])
                confirmed.append("sanitizer_probe")
                stages.append("S5_verify")
            dom["partitions"] = _int_partitions(o["default"], lo, hi)
            dom["rationale"] = "heuristic partitions around the default and the probed lower bound"
        ev = [{"file": o["definition"]["file"], "lines": cm.format_lines(o["definition"]["lines"]),
               "kind": "definition"}]
        by_file = {}
        for u in o["uses"]:
            by_file.setdefault(u["file"], []).append(u["line"])
        for f, ls in by_file.items():
            ev.append({"file": f, "lines": cm.format_lines(ls), "kind": "use"})
        if docs.get(n):
            ev.append({"file": docs[n][0]["file"], "lines": cm.format_lines(h["line"] for h in docs[n]),
                       "kind": "documentation"})
        if al.get("options_line"):
            ev.append({"file": "meson_options.txt", "lines": str(al["options_line"]), "kind": "build_system"})
        rec = {"name": n, "binding": "compile_time", "description": desc, "domain": dom,
               "default": o["default"], "default_source": "code"}
        aw = o.get("active_when")
        status = "confirmed"
        iv = inert.get(n, {})
        if aw:
            if iv.get("status") == "confirmed":
                confirmed.append("preprocess_equivalence")
                rec["active_when"] = aw
            elif iv:
                status = "hypothesis"
                rec["active_when"] = aw
            else:
                rec["active_when"] = aw
        elif iv.get("status") == "suspect":
            status = "hypothesis"
        if "S5_verify" not in stages and iv:
            stages.append("S5_verify")
        fmt = {"boolean": "bool01", "integer": "int", "string": "c_string_literal"}[o["type"]]
        rec["apply"] = {"mechanism": "c_macro", "macro": n, "format": fmt}
        rec["evidence"] = ev
        if al.get("meson_option"):
            rec["build_system_alias"] = al["meson_option"] + (" (inverted)" if al.get("inverted") else "")
        rec["provenance"] = {"discovered_by": ["static"] + (["empirical"] if confirmed else []),
                             "stages": stages, "confirmed_by": confirmed or ["source_inspection"],
                             "status": status}
        options.append(rec)

    for c in (s3 or {}).get("candidates", []):
        if not c.get("values"):
            continue
        vals = [{"value": v, "token": csm_tool.derive_token(v, i)} if not csm_tool._SAFE.match(str(v)) else v
                for i, v in enumerate(c["values"])]
        options.append({
            "name": c["name"], "binding": "runtime", "description": c["description"],
            "domain": {"type": "enum", "values": vals}, "default": c["default"],
            "default_source": c.get("default_source", "convention"),
            "apply": c.get("apply", {"mechanism": "api_argument"}),
            "evidence": c["evidence"],
            "provenance": {"discovered_by": ["static"], "stages": ["S3_runtime"],
                           "notes": f"rule: {c['rule']}", "status": "hypothesis"}})

    constraints = []
    for i, c in enumerate(s1.get("constraint_candidates", [])):
        if not c["options"]:
            continue
        constraints.append({"id": f"S1_E{i + 1}", "kind": "hard", "expr": "false", "origin": "code",
                            "status": "hypothesis", "rationale": f"#error: {c['message']}",
                            "evidence": [{"file": c["file"], "lines": str(c["line"]), "kind": "definition"}]})
    if s4:
        by_name = {o["name"]: o for o in options}
        for o in s4.get("options", []):
            if o.get("update") and o["name"] in by_name:
                tgt = by_name[o["name"]]
                if "domain" in o["update"]:
                    tgt["domain"] = o["update"]["domain"]
                if o["update"].get("description"):
                    tgt["description"] = o["update"]["description"]
                pv = tgt.setdefault("provenance", {})
                pv["discovered_by"] = sorted(set(pv.get("discovered_by", [])) | {"llm"})
                pv["stages"] = list(dict.fromkeys(pv.get("stages", []) + ["S4_llm"]))
                pv["model"] = s4.get("model")
            elif o.get("record") and o["name"] not in by_name:
                options.append(o["record"])
                by_name[o["name"]] = o["record"]
        s5c = (s5 or {}).get("constraints", {})
        for c in s4.get("constraints", []):
            rec = dict(c["record"])
            res = s5c.get(rec["id"], {})
            pv = dict(rec.get("provenance", {}))
            if res.get("status") == "confirmed_empirically":
                rec["status"] = "confirmed"
                pv["confirmed_by"] = ["build_probe"]
                rec["evidence"] = rec["evidence"] + [{"file": "ini.c", "kind": "empirical",
                                                      "note": f"violating {res['violating']}: {res['outcome']}"}]
            elif rec.get("origin") == "documented":
                # evidence already verified by the S4 guards (G3): documentary support
                rec["status"] = "confirmed"
                pv["confirmed_by"] = ["documentation"]
                if res.get("status") == "no_failure_observed":
                    pv["notes"] = f"violating {res['violating']} builds and runs clean; documentary constraint"
            pv["status"] = rec["status"]
            rec["provenance"] = pv
            constraints.append(rec)
    groups = []
    for g in s1.get("groups", []):
        groups.append({"id": g["id"], "options": g["options"], "reason": g["reason"],
                       "region": {"file": g["region"]["file"], "lines": cm.format_lines(g["region"]["lines"])},
                       "evidence": [{"file": g["region"]["file"], "lines": cm.format_lines(g["region"]["lines"]),
                                     "kind": "use"}],
                       "suggested_strength": g["suggested_strength"],
                       "provenance": {"discovered_by": ["static"], "stages": ["S1_cpp"], "status": "hypothesis"}})
    if s4:
        groups += [g["record"] for g in s4.get("groups", [])]

    excluded = [{"option_name": e["option_name"], "reason": e["reason"], "evidence": e["evidence"]}
                for e in s1.get("excluded", [])]
    mapped = {a["meson_option"] for a in aliases.values()}
    for mo, info in (meson.get("options") or {}).items():
        if mo not in mapped:
            excluded.append({"option_name": f"meson:{mo}",
                             "reason": "build-system option with no -D macro mapping (packaging/orchestration)",
                             "evidence": [{"file": "meson_options.txt", "lines": str(info["line"]),
                                           "kind": "build_system"}]})
    masked = (meson.get("mapping") or {}).get("masked_when_distro_install")
    model = {"csm_version": "2.0",
             "system": dict((harness or {}).get("system", {"name": "unknown", "repository": "", "commit": "",
                                                          "language": "C"})),
             "options": options, "constraints": constraints, "interaction_groups": groups,
             "excluded_options": excluded}
    model["system"]["extraction"] = {"date": str(date.today()), "method": "discover pipeline S1-S6",
                                     "masked_feature_flags_when_distro_install": masked or []}
    if harness:
        model["build"] = harness["build"]
        suite = json.loads(json.dumps(harness["test_suite"]))
        known = {o["name"] for o in options}

        def ok(cond) -> bool:
            try:
                return cm.referenced_options(cm.as_condition(cond)) <= known
            except ValueError:
                return False
        for d in suite.get("drivers", []):
            for key in ("compatible_when", "baseline_valid_when"):
                if key in d and not ok(d[key]):
                    d.pop(key)
            d["extra_link_sources_when"] = [r for r in d.get("extra_link_sources_when", []) if ok(r["when"])]
            d.pop("fixed_runtime", None)
        model["test_suite"] = suite
    return model


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("what", choices=["view", "model"])
    ap.add_argument("--s1", required=True)
    ap.add_argument("--s2")
    ap.add_argument("--s3")
    ap.add_argument("--s4")
    ap.add_argument("--s5")
    ap.add_argument("--harness", help="model whose system/build/test_suite sections are copied")
    ap.add_argument("-o", "--output", required=True)
    a = ap.parse_args(argv)
    s1, s2, s3, s4, s5 = (_load(a.s1), _load(a.s2), _load(a.s3), _load(a.s4), _load(a.s5))
    if a.what == "view":
        Path(a.output).write_text(json.dumps(build_view(s1, s2, s3, s4), indent=2))
        return 0
    harness = None
    if a.harness:
        raw = cm.read_model_file(a.harness)
        harness = {"system": raw["system"], "build": raw.get("build"), "test_suite": raw.get("test_suite")}
    model = build_model(s1, s2, s3, s4, s5, harness)
    cm.schema_validate(model)
    header = "# CSM v2 produced by the discovery pipeline (discover/run_discovery.py). Machine-generated.\n"
    Path(a.output).write_text(csm_tool.dump_yaml(model, header))
    print(f"wrote {a.output}: {len(model['options'])} options, {len(model['constraints'])} constraints, "
          f"{len(model['interaction_groups'])} groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
