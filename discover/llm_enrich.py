#!/usr/bin/env python3
"""S4 - LLM enrichment: prompt preparation and guarded ingestion.

  llm_enrich.py prepare --root SUBJECT --s1 s1.json --s2 s2.json --s3 s3.json
                        --draft csm_discovered.yaml -o prompts/ [--chunk-lines 220]
      writes prompt_NN.md files (chunked source/doc excerpts + task + output
      contract) for an LLM-based agent (Claude Code, or another model for
      cross-checking).

  llm_enrich.py ingest  --root SUBJECT --draft csm_discovered.yaml
                        --response answer.yaml --model "<model id>" -o s4.json
      validates the agent's YAML answer and keeps only what passes the guards:
        G1 structure     required fields, known kinds, CSM-parsable expressions
        G2 names         compile-time options must exist in the sources; updates
                         must target known options
        G3 evidence      cited files/lines exist and mention the element
        G4 values        literals in expressions are legal for the option
      Rejected items are kept in s4.json with the reason (hallucination counts
      for RQ1). Accepted items enter the model with status 'hypothesis' and
      provenance discovered_by: [llm].
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import csm_model as cm  # noqa: E402

import yaml  # noqa: E402

OUTPUT_CONTRACT = """\
Answer with ONE fenced ```yaml block and nothing else inside it. Top-level keys (all optional):

option_updates:            # refine options that are already in the draft model
  - name: <existing option name>
    description: <one or two sentences>                 # optional
    domain:                                             # optional; full replacement of the domain
      type: boolean | enum | integer | string
      values: [...]                                     # boolean/enum/string: {value, token?, rationale}
      range: {min: <int>, max: <int>}                   # integer
      partitions:                                       # integer: inclusive intervals covering the range
        - {label: <name>, interval: [<lo>, <hi or "max">], representative: <int>, rationale: <why>}
      rationale: <why these classes>
    evidence: [{file: <path>, lines: "<a-b,c>", kind: definition|use|documentation|build_system}]

new_options:               # variability points missing from the draft (typically runtime options)
  - name: <identifier>
    binding: compile_time | runtime
    description: <text>
    domain: {...}                                       # as above
    default: <value>
    default_source: code | docs | build_system | convention
    apply: {mechanism: c_macro | driver_cli | api_argument | env_var | config_file, ...}
    evidence: [...]                                     # REQUIRED

constraints:
  - id: <short id>
    kind: hard | soft          # hard: configuration invalid; soft: legal but contradictory
    expr: <CSM expression>     # e.g. if A == 1 and B == 0 then C <= D ; A in [1, 2]
    origin: documented | code | empirical | llm
    rationale: <why>
    evidence: [...]            # REQUIRED: lines that state or imply the constraint

interaction_groups:
  - id: <short id>
    options: [<opt>, <opt>, ...]
    reason: shared_code | nested_ifdef | shared_condition | shared_function | documented
    region: {file: <path>, lines: "<a-b>"}
    suggested_strength: 2 | 3
    evidence: [...]

Rules
- Use only option names that appear in the draft model or that you declare under new_options.
- Every evidence entry must point at real lines in the excerpts; quote nothing you have not seen.
- Booleans are 0/1. String/enum literals are double-quoted in expressions.
- Prefer fewer, well-evidenced items over many speculative ones.
"""


def _excerpt(root: Path, file: str, lines: set[int], ctx: int = 2) -> list[tuple[int, str]]:
    text = (root / file).read_text(errors="replace").splitlines()
    want = set()
    for ln in lines:
        want |= set(range(max(1, ln - ctx), min(len(text), ln + ctx) + 1))
    return [(i, text[i - 1]) for i in sorted(want)]


def prepare(root: Path, s1: dict, s2: dict | None, s3: dict | None, draft_path: Path, out_dir: Path,
            chunk_lines: int = 220) -> list[Path]:
    draft = cm.load_csm(draft_path)
    wanted: dict[str, set[int]] = {}
    for o in s1["options"]:
        f = o["definition"]["file"]
        wanted.setdefault(f, set()).update(o["definition"]["lines"])
        if o.get("doc_lines"):
            wanted[f].update(range(o["doc_lines"][0], o["doc_lines"][1] + 1))
        for u in o["uses"]:
            wanted.setdefault(u["file"], set()).add(u["line"])
    for l in s1.get("leads", []):
        wanted.setdefault(l["file"], set()).add(l["line"])
    for name, hits in ((s2 or {}).get("docs") or {}).items():
        for h in hits:
            wanted.setdefault(h["file"], set()).add(h["line"])
    for c in (s3 or {}).get("candidates", []):
        for e in c["evidence"]:
            for ln in cm.parse_lines(e["lines"]):
                wanted.setdefault(e["file"], set()).add(ln)

    blocks = []
    for f in sorted(wanted):
        ex = _excerpt(root, f, wanted[f])
        cur, prev = [], None
        for ln, txt in ex:
            if prev is not None and ln != prev + 1:
                cur.append("   ...")
            cur.append(f"{ln:5d}| {txt}")
            prev = ln
        blocks.append((f, cur))

    summary = []
    for n in draft.order:
        o = draft.options[n]
        aw = f"; active_when: {cm.condition_to_text(o['active_when'])}" if "active_when" in o else ""
        summary.append(f"- {n} ({o['binding_time']}, {o['domain_type']}, default {o['default_value']!r}; "
                       f"settings {draft.settings(n)}{aw}; status {o.get('provenance', {}).get('status')})")
    leads = [f"- {l['file']}:{l['line']} mentions {', '.join(l['options'])}: \"{l['text']}\""
             for l in s1.get("leads", [])]

    header = f"""# Configuration-space enrichment task (stage S4)

You are assisting an empirical study on combinatorial interaction testing of the configurable
C library at `{root.name}`. Static analysis (preprocessor structure, build files, API scan) has
produced the draft model below. Your job is the part static analysis cannot do:

1. **Constraints**: find option combinations that are invalid (hard) or legal but contradictory (soft).
   Comments and documentation are the main source; cite them.
2. **Discretisation**: for integer and string options, propose equivalence classes and
   representative values that are meaningful for testing (explain each class).
3. **Runtime options**: confirm, rename or add runtime variability points (API choices, callback
   contracts, CLI flags) a test driver can vary without rebuilding.
4. **High-risk interactions**: groups of options that jointly control the same code.

## Draft model
{chr(10).join(summary)}

## Static leads (comments mentioning several options)
{chr(10).join(leads) if leads else "- none"}

## Output contract
{OUTPUT_CONTRACT}
"""
    out_dir.mkdir(parents=True, exist_ok=True)
    files, part, cur_len, chunk = [], 1, 0, []
    for f, lines in blocks:
        for i in range(0, len(lines), chunk_lines):
            piece = lines[i:i + chunk_lines]
            if cur_len + len(piece) > chunk_lines and chunk:
                files.append(_write_part(out_dir, part, header, chunk))
                part, cur_len, chunk = part + 1, 0, []
            chunk.append((f, piece))
            cur_len += len(piece)
    if chunk:
        files.append(_write_part(out_dir, part, header, chunk))
    return files


def _write_part(out_dir: Path, part: int, header: str, chunk) -> Path:
    body = [header, f"## Source excerpts (part {part})", ""]
    for f, lines in chunk:
        body += [f"### {f}", "```", *lines, "```", ""]
    p = out_dir / f"prompt_{part:02d}.md"
    p.write_text("\n".join(body))
    return p


# --------------------------------------------------------------------------
# Ingestion with guards
# --------------------------------------------------------------------------

class Guard:
    def __init__(self, root: Path, draft: cm.CSM, sources: list[str]):
        self.root, self.draft = root, draft
        self.source_text = "\n".join((root / s).read_text(errors="replace") for s in sources)
        self.cache = {}

    def file_lines(self, f: str) -> list[str] | None:
        if f not in self.cache:
            p = (self.root / f)
            try:
                p.resolve().relative_to(self.root.resolve())
            except ValueError:
                self.cache[f] = None
                return None
            self.cache[f] = p.read_text(errors="replace").splitlines() if p.is_file() else None
        return self.cache[f]

    def evidence(self, ev: list, mention: set[str]) -> str | None:
        """None if OK, else the reason for rejection."""
        if not ev:
            return "G3: no evidence"
        mentioned = False
        for e in ev:
            if not isinstance(e, dict) or "file" not in e:
                return "G1: malformed evidence entry"
            lines = self.file_lines(e["file"])
            if lines is None:
                return f"G3: cited file does not exist: {e['file']}"
            try:
                nums = cm.parse_lines(e.get("lines"))
            except ValueError:
                return f"G3: unparsable line spec {e.get('lines')!r}"
            if not nums:
                return "G3: evidence without line numbers"
            if max(nums) > len(lines):
                return f"G3: {e['file']} has {len(lines)} lines, cited {max(nums)}"
            # allow the mention within 3 lines of the cited span (doc comments precede definitions)
            window = "\n".join(lines[max(0, min(nums) - 4):min(len(lines), max(nums) + 3)])
            if not mention or any(re.search(rf"(?:\b|(?<=-D)){re.escape(m)}\b", window) for m in mention):
                mentioned = True
        return None if mentioned else f"G3: cited lines never mention {sorted(mention)}"


def ingest(root: Path, draft_path: Path, response_text: str, model_id: str, sources: list[str]) -> dict:
    draft = cm.load_csm(draft_path)
    m = re.search(r"```ya?ml\s*\n(.*?)```", response_text, re.S)
    body = m.group(1) if m else response_text
    try:
        resp = yaml.safe_load(body) or {}
    except yaml.YAMLError as e:
        return {"stage": "S4_llm", "model": model_id, "error": f"G1: unparsable YAML: {e}",
                "options": [], "constraints": [], "groups": [], "rejected": []}
    g = Guard(root, draft, sources)
    prov = {"discovered_by": ["llm"], "model": model_id, "stages": ["S4_llm"], "status": "hypothesis"}
    out = {"stage": "S4_llm", "model": model_id, "options": [], "constraints": [], "groups": [],
           "rejected": []}
    known = dict(draft.options)

    def reject(kind, item, reason):
        out["rejected"].append({"kind": kind, "item": item, "reason": reason})

    # new options first (so constraints may reference them)
    for o in resp.get("new_options") or []:
        name = o.get("name") if isinstance(o, dict) else None
        if not name or not re.match(r"^[A-Za-z_]\w*$", str(name)):
            reject("new_option", o, "G1: missing or invalid name")
            continue
        if name in known:
            reject("new_option", o, "G2: already in the draft (use option_updates)")
            continue
        if o.get("binding") == "compile_time" and not re.search(rf"\b{re.escape(name)}\b", g.source_text):
            reject("new_option", o, "G2: compile-time option name does not occur in the sources (hallucinated)")
            continue
        mention = {name}
        dom = o.get("domain") or {}
        mention |= {str(v.get("value") if isinstance(v, dict) else v) for v in dom.get("values", [])}
        reason = g.evidence(o.get("evidence"), mention)
        if reason:
            reject("new_option", o, reason)
            continue
        rec = {k: o[k] for k in ("name", "binding", "description", "domain", "default", "default_source",
                                 "apply", "evidence") if k in o}
        rec.setdefault("description", "")
        rec.setdefault("apply", {"mechanism": "api_argument" if o.get("binding") == "runtime" else "c_macro"})
        rec["provenance"] = dict(prov)
        try:
            cm.schema_validate({"csm_version": "2.0", "system": {"name": "x", "repository": "", "commit": "",
                                                                 "language": "C"},
                                "options": [rec], "constraints": []})
        except Exception as e:  # noqa: BLE001
            reject("new_option", o, f"G1: schema: {str(e).splitlines()[0][:160]}")
            continue
        vals = [v.get("value") if isinstance(v, dict) else v for v in dom.get("values", [])] or \
               [p["representative"] for p in dom.get("partitions", [])]
        out["options"].append({"name": name, "type": dom.get("type"), "default": o.get("default"),
                               "binding": o.get("binding"), "settings": vals, "record": rec})
        known[name] = {"domain_type": dom.get("type")}

    for u in resp.get("option_updates") or []:
        name = u.get("name") if isinstance(u, dict) else None
        if name not in draft.options:
            reject("option_update", u, "G2: unknown option")
            continue
        if u.get("evidence"):
            reason = g.evidence(u["evidence"], {name})
            if reason:
                reject("option_update", u, reason)
                continue
        out["options"].append({"name": name, "update": {k: u[k] for k in ("description", "domain") if k in u},
                               "settings": [p["representative"] for p in (u.get("domain") or {}).get("partitions", [])]
                               or [v.get("value") if isinstance(v, dict) else v
                                   for v in (u.get("domain") or {}).get("values", [])] or None,
                               "record": None})

    for c in resp.get("constraints") or []:
        if not isinstance(c, dict) or not all(k in c for k in ("id", "kind", "expr")):
            reject("constraint", c, "G1: missing id/kind/expr")
            continue
        if c["kind"] not in ("hard", "soft"):
            reject("constraint", c, "G1: kind must be hard or soft")
            continue
        try:
            f = cm.parse_expression(str(c["expr"]), known)
        except ValueError as e:
            reject("constraint", c, f"G1/G2: {e}")
            continue
        bad = None
        for atom in _atoms(f):
            o = draft.options.get(atom["option"])
            if o and "value" in atom and o["domain_type"] in ("boolean", "enum") \
                    and atom["value"] not in draft.settings(atom["option"]):
                bad = f"G4: {atom['option']} has no value {atom['value']!r}"
        if bad:
            reject("constraint", c, bad)
            continue
        reason = g.evidence(c.get("evidence"), cm.referenced_options(f))
        if reason:
            reject("constraint", c, reason)
            continue
        rec = {"id": f"L_{c['id']}", "kind": c["kind"], "expr": str(c["expr"]),
               "origin": c.get("origin", "llm") if c.get("origin") in ("documented", "code", "empirical", "llm") else "llm",
               "status": "hypothesis", "rationale": c.get("rationale", ""), "evidence": c["evidence"],
               "provenance": dict(prov)}
        out["constraints"].append({"id": rec["id"], "kind": rec["kind"], "expr": rec["expr"], "record": rec})

    for gr in resp.get("interaction_groups") or []:
        opts = gr.get("options") if isinstance(gr, dict) else None
        if not opts or any(o not in known for o in opts) or len(set(opts)) < 2:
            reject("group", gr, "G2: unknown or too few options")
            continue
        if gr.get("evidence"):
            reason = g.evidence(gr["evidence"], set(opts))
            if reason:
                reject("group", gr, reason)
                continue
        rec = {"id": f"L_{gr.get('id', len(out['groups']) + 1)}", "options": sorted(set(opts)),
               "reason": gr.get("reason", "shared_code"),
               "suggested_strength": int(gr.get("suggested_strength", 2)),
               "provenance": dict(prov)}
        if gr.get("region"):
            rec["region"] = gr["region"]
        if gr.get("evidence"):
            rec["evidence"] = gr["evidence"]
        out["groups"].append({"record": rec})

    proposed = sum(len(resp.get(k) or []) for k in ("new_options", "option_updates", "constraints",
                                                     "interaction_groups"))
    out["stats"] = {"proposed": proposed, "accepted": proposed - len(out["rejected"]),
                    "rejected": len(out["rejected"]),
                    "rejected_by_guard": _count(r["reason"].split(":")[0] for r in out["rejected"])}
    return out


def _atoms(f, acc=None):
    acc = [] if acc is None else acc
    if isinstance(f, bool):
        return acc
    for k in ("all", "any", "implies"):
        if k in f:
            for x in f[k]:
                _atoms(x, acc)
            return acc
    if "not" in f:
        return _atoms(f["not"], acc)
    acc.append(f)
    return acc


def _count(xs):
    d = {}
    for x in xs:
        d[x] = d.get(x, 0) + 1
    return d


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--root", required=True)
    p.add_argument("--s1", required=True)
    p.add_argument("--s2")
    p.add_argument("--s3")
    p.add_argument("--draft", required=True)
    p.add_argument("--chunk-lines", type=int, default=220)
    p.add_argument("-o", "--out-dir", required=True)
    q = sub.add_parser("ingest")
    q.add_argument("--root", required=True)
    q.add_argument("--draft", required=True)
    q.add_argument("--response", required=True)
    q.add_argument("--model", required=True)
    q.add_argument("--sources", nargs="+", default=["ini.c", "ini.h"])
    q.add_argument("-o", "--output", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "prepare":
        ld = lambda x: json.loads(Path(x).read_text()) if x else None  # noqa: E731
        files = prepare(Path(a.root), ld(a.s1), ld(a.s2), ld(a.s3), Path(a.draft), Path(a.out_dir), a.chunk_lines)
        print("\n".join(map(str, files)))
        return 0
    res = ingest(Path(a.root), Path(a.draft), Path(a.response).read_text(), a.model, a.sources)
    Path(a.output).write_text(json.dumps(res, indent=2, default=str))
    print(json.dumps(res.get("stats", res.get("error")), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
