#!/usr/bin/env python3
"""Validate a design graph and ordered examples, then render its documentation.

Exact edge coverage, per-instance sequencing and declared cross-instance guards are
checked. Graph reachability is existential; this is not a runtime model checker or
a proof that cleanup eventually runs. Explicit initial states are assumptions.
"""
import copy
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACTORS = {"human", "model", "blinkd", "scheduler", "browser", "service"}
T_RE = re.compile(r"^(?P<src>[a-z][a-z0-9_]*)>(?P<dst>[a-z][a-z0-9_]*):(?P<trigger>[a-z0-9_]+)@(?P<actor>[a-z]+)$")
I_RE = re.compile(r"^(?P<entity>[A-Za-z][A-Za-z0-9_]*)#[a-zA-Z0-9_-]+$")


def reach(edges, starts):
    seen, todo = set(starts), list(starts)
    while todo:
        for dst in edges.get(todo.pop(), ()):
            if dst not in seen:
                seen.add(dst)
                todo.append(dst)
    return seen


def parse(model):
    errors, ents = [], {}
    if not isinstance(model, dict) or not isinstance(model.get("entities"), dict):
        return {}, ["E1 model.entities must be an object"]
    if not model["entities"]:
        errors.append("E1 model.entities must not be empty")
    for name, ent in model["entities"].items():
        if not isinstance(ent, dict):
            errors.append(f"E1 {name}: entity must be an object")
            continue
        if not isinstance(ent.get("desc", ""), str):
            errors.append(f"E1 {name}: description must be text")
        recovery = ent.get("recovery", {})
        if not isinstance(recovery, dict) or not all(isinstance(v, str) for v in recovery.values()):
            errors.append(f"E1 {name}: recovery must be an object of text contracts")
        trans, seen = [], set()
        raw_trans = ent.get("transitions", [])
        if not isinstance(raw_trans, list):
            errors.append(f"E1 {name}: transitions must be a list")
            raw_trans = []
        for raw in raw_trans:
            m = T_RE.fullmatch(raw) if isinstance(raw, str) else None
            if not m:
                errors.append(f"E1 {name}: malformed transition {raw!r}")
                continue
            if m["actor"] not in ACTORS:
                errors.append(f"E1 {name}: unknown actor in {raw!r}")
                continue
            if m["dst"] == "new":
                errors.append(f"E1 {name}: new is a creation source only")
                continue
            if raw in seen:
                errors.append(f"E1 {name}: duplicate transition {raw!r}")
                continue
            seen.add(raw)
            trans.append(dict(m.groupdict(), raw=raw))
        terminals = ent.get("terminal", [])
        if not isinstance(terminals, list) or not all(isinstance(s, str) for s in terminals):
            errors.append(f"E2 {name}: terminal must be a list of state names")
            terminals = []
        states = {t[k] for t in trans for k in ("src", "dst")} - {"new"}
        ents[name] = dict(terminal=set(terminals), trans=trans, states=states,
                          desc=ent.get("desc", ""), guards=copy.deepcopy(ent.get("guards", {})),
                          recovery=ent.get("recovery", {}))
    for name, ent in ents.items():
        guards = ent["guards"]
        if not isinstance(guards, dict):
            errors.append(f"E9 {name}: guards must be an object")
            ent["guards"] = {}
            continue
        exact = {t["raw"] for t in ent["trans"]}
        for edge, rules in list(guards.items()):
            if edge not in exact or not isinstance(rules, list):
                errors.append(f"E9 {name}: invalid guarded edge {edge!r}")
                guards[edge] = []
                continue
            valid = []
            for rule in rules:
                target = (ents.get(rule["entity"]) if isinstance(rule, dict)
                          and isinstance(rule.get("entity"), str) else None)
                states = rule.get("states") if isinstance(rule, dict) else None
                if (not target or not isinstance(rule.get("role"), str)
                        or not isinstance(states, list) or not states
                        or not all(isinstance(s, str) and s in target["states"] for s in states)
                        or not isinstance(rule.get("same_scope", False), bool)
                        or not isinstance(rule.get("distinct_instance", False), bool)
                        or ("reciprocal_role" in rule and
                            (not isinstance(rule["reciprocal_role"], str) or not rule["reciprocal_role"]))):
                    errors.append(f"E9 {name}: invalid guard for {edge!r}: {rule!r}")
                    continue
                valid.append(rule)
            guards[edge] = valid
    return ents, errors


def check(model):
    ents, errors = parse(model)
    for name, ent in ents.items():
        fwd, rev = defaultdict(set), defaultdict(set)
        for t in ent["trans"]:
            fwd[t["src"]].add(t["dst"])
            rev[t["dst"]].add(t["src"])
        if not fwd.get("new"):
            errors.append(f"E2 {name}: no creation transition")
        if not ent["terminal"]:
            errors.append(f"E2 {name}: no terminal state")
        for state in sorted(ent["terminal"] - ent["states"]):
            errors.append(f"E2 {name}: terminal {state!r} never entered")
        for state in sorted(ent["states"] - reach(fwd, ["new"])):
            errors.append(f"E3 {name}: state {state!r} unreachable from creation")
        for state in sorted(ent["states"] - reach(rev, ent["terminal"]) - ent["terminal"]):
            errors.append(f"E4 {name}: state {state!r} has no graph path to a terminal")
        for state in sorted(ent["terminal"]):
            if fwd.get(state):
                errors.append(f"E5 {name}: terminal {state!r} has outgoing transitions")

    covered = set()
    flows = model.get("flows", {}) if isinstance(model, dict) else {}
    examples = model.get("edge_examples", {}) if isinstance(model, dict) else {}
    if not isinstance(flows, dict) or not isinstance(examples, dict):
        errors.append("E6 flows and edge_examples must be objects")
        flows, examples = {}, {}
    if not flows:
        errors.append("E6 at least one ordered scenario is required")
    for kind, entries in (("scenario", flows), ("edge example", examples)):
        for flow, spec in entries.items():
            prefix = f"{kind} {flow}"
            if not isinstance(spec, dict) or not isinstance(spec.get("steps"), list):
                errors.append(f"E6 {prefix}: expected an object with steps")
                continue
            if not isinstance(spec.get("purpose", ""), str):
                errors.append(f"E6 {prefix}: purpose must be text")
            given, links, scopes = (spec.get(k, {}) for k in ("given", "links", "scopes"))
            if not all(isinstance(x, dict) for x in (given, links, scopes)):
                errors.append(f"E6 {prefix}: given, links and scopes must be objects")
                continue
            current, instances = {}, {}

            def register(instance):
                match = I_RE.fullmatch(instance) if isinstance(instance, str) else None
                if not match or match["entity"] not in ents:
                    errors.append(f"E6 {prefix}: unknown or malformed instance {instance!r}")
                    return None
                entity = match["entity"]
                instances[instance] = entity
                current.setdefault(instance, "new")
                return entity

            for instance, state in given.items():
                entity = register(instance)
                if entity and (not isinstance(state, str) or state not in ents[entity]["states"] | {"new"}):
                    errors.append(f"E8 {prefix}: invalid initial state {instance}={state!r}")
                elif entity:
                    current[instance] = state
            for owner, roles in links.items():
                register(owner)
                if not isinstance(roles, dict):
                    errors.append(f"E9 {prefix}: links for {owner!r} must be an object")
                    continue
                for target in roles.values():
                    register(target)
            for instance, scope in scopes.items():
                register(instance)
                if not isinstance(scope, str) or not scope:
                    errors.append(f"E9 {prefix}: scope of {instance!r} must be a nonempty string")
            for index, step in enumerate(spec["steps"], 1):
                if not isinstance(step, str) or ":" not in step:
                    errors.append(f"E6 {prefix} step {index}: malformed step {step!r}")
                    continue
                instance, edge = step.split(":", 1)
                entity = register(instance)
                if not entity:
                    continue
                transition = next((t for t in ents[entity]["trans"] if t["raw"] == edge), None)
                if transition is None:
                    errors.append(f"E6 {prefix} step {index}: unknown exact transition {step!r}")
                    continue
                if current[instance] != transition["src"]:
                    errors.append(f"E8 {prefix} step {index}: {instance} is {current[instance]!r}, expected {transition['src']!r}")
                    continue
                guard_ok = True
                for guard in ents[entity]["guards"].get(edge, []):
                    roles = links.get(instance, {})
                    target = roles.get(guard["role"]) if isinstance(roles, dict) else None
                    if (not isinstance(target, str) or target not in instances or instances[target] != guard["entity"]
                            or current[target] not in guard["states"]):
                        errors.append(f"E9 {prefix} step {index}: {instance} requires linked {guard['role']} ({guard['entity']}) in {guard['states']}")
                        guard_ok = False
                    elif guard.get("distinct_instance") and target == instance:
                        errors.append(f"E9 {prefix} step {index}: {instance} requires a distinct {guard['role']} instance")
                        guard_ok = False
                    elif guard.get("same_scope") and (not scopes.get(instance) or scopes.get(instance) != scopes.get(target)):
                        errors.append(f"E9 {prefix} step {index}: {instance} and {target} must share an explicit scope")
                        guard_ok = False
                    elif guard.get("reciprocal_role") and (
                            not isinstance(links.get(target), dict)
                            or links[target].get(guard["reciprocal_role"]) != instance):
                        errors.append(f"E9 {prefix} step {index}: {target} must bind its {guard['reciprocal_role']} role to {instance}")
                        guard_ok = False
                if guard_ok:
                    covered.add((entity, edge))
                    current[instance] = transition["dst"]
            if kind == "edge example" and len(spec["steps"]) != 1:
                errors.append(f"E6 {prefix}: an isolated edge example must contain exactly one step")
    total = sum(len(e["trans"]) for e in ents.values())
    for name, ent in ents.items():
        for t in ent["trans"]:
            if (name, t["raw"]) not in covered:
                errors.append(f"E7 {name}: exact transition {t['raw']} not exercised")
    return ents, errors, total, len(covered)


def render(model, ents, total, covered):
    out = ["# Web Blink: Interaction and Lifecycle Flow Map", "",
           "Generated by `tools/check_flows.py` from `design/flows.json`; do not edit by hand.", "",
           f"Validation: **passed**. {len(ents)} entities; {covered}/{total} exact transitions exercised by "
           f"{len(model['flows'])} ordered scenarios and {len(model.get('edge_examples', {}))} isolated edge examples.", "",
           "Checks cover full transition identity (including trigger and actor), state progression per instance, "
           "valid initial states, declared linked-instance prerequisites and matching scopes. Every modelled "
           "state is reachable from creation and has some graph path to a terminal state.", "",
           "**Limits:** initial states and matching scope labels are scenario assumptions. Edge examples "
           "exercise one transition, not an integrated workflow. Existential graph reachability does not "
           "prove eventual cleanup, global completeness, transaction atomicity, actual provider behavior or "
           "runtime enforcement. Human action, deadlines, fair scheduling and successful storage are not proven. "
           "Recovery contracts below are implementation requirements, not measured results.", "",
           "## Component interaction graph", "", "```mermaid", "flowchart LR",
           "  H([Human]) -- Shared browser --> CH[Shared Chrome]",
           "  H -- requests and answers --> M([Model])",
           "  M -- meta tools --> MCP[Local MCP server]",
           "  MCP -- local transport --> D[blinkd]",
           "  PF[Capability preflight] --> MCP",
           "  PF --> D",
           "  D --> REG[(Registry and grants)]",
           "  H -- named Git URL and ref --> SRC[Configured Web Link sources]",
           "  SRC --> READ[Bounded immutable Git blob reader]",
           "  READ --> CAT[Validated Web Link catalog]",
           "  CAT -- quarantined candidates --> REG",
           "  D --> J[(Journal and reservations)]",
           "  D --> S[(Secret store)]",
           "  D --> A[(Value-free audit)]",
           "  D -- scoped browser connection --> CH",
           "  D -- granted HTTPS --> SVC[Service backends]",
           "  CH -- in-page requests --> SVC",
           "  SCH[Scheduler] --> D",
           "  H -- out-of-band receipts --> D",
           "  D -- notifications --> H", "```", "", "## Entity lifecycles", ""]
    for name, ent in ents.items():
        out += [f"### {name}", "", ent["desc"], "", "```mermaid", "stateDiagram-v2"]
        for t in ent["trans"]:
            src = "[*]" if t["src"] == "new" else t["src"]
            out.append(f"  {src} --> {t['dst']}: {t['trigger']} ({t['actor']})")
        out += [f"  {s} --> [*]" for s in sorted(ent["terminal"])]
        out += ["```", ""]
        if ent["recovery"]:
            out += ["Recovery contract: " + "; ".join(f"**{k}**: {v}" for k, v in ent["recovery"].items()), ""]
        if ent["guards"]:
            out += ["Declared prerequisites:", ""]
            for edge, guards in ent["guards"].items():
                out.append(f"- `{edge}`: " + "; ".join(
                    f"linked `{g['role']}` ({g['entity']}) in {', '.join(g['states'])}"
                    + (", same scope" if g.get("same_scope") else "")
                    + (", distinct instance" if g.get("distinct_instance") else "")
                    + (f", reciprocal `{g['reciprocal_role']}` binding" if g.get("reciprocal_role") else "")
                    for g in guards))
            out.append("")
    for heading, key in (("Ordered scenarios", "flows"), ("Isolated edge coverage examples", "edge_examples")):
        out += [f"## {heading}", ""]
        if key == "edge_examples":
            out += ["These local examples assert their starting states and prerequisites. They provide exact-edge "
                    "coverage only; they do not establish integrated execution order across those prerequisites.", ""]
        for name, flow in model.get(key, {}).items():
            out += [f"### {name}", "", flow.get("purpose", ""), ""]
            if flow.get("given"):
                out += ["Assumed initial states: " + ", ".join(f"`{k}={v}`" for k, v in flow["given"].items()), ""]
            if flow.get("links"):
                out += ["Links: `" + json.dumps(flow["links"], sort_keys=True) + "`", ""]
            if flow.get("scopes"):
                out += ["Scopes: `" + json.dumps(flow["scopes"], sort_keys=True) + "`", ""]
            out += [f"{i}. `{step}`" for i, step in enumerate(flow["steps"], 1)] + [""]
    return "\n".join(out)


def main():
    try:
        model = json.loads((ROOT / "design/flows.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAILED: cannot read model: {exc}")
        return 1
    ents, errors, total, covered = check(model)
    if errors:
        print("\n".join(errors))
        print(f"FAILED: {len(errors)} problem(s)")
        return 1
    (ROOT / "docs/05-flow-map.md").write_text(render(model, ents, total, covered))
    print(f"OK: {len(ents)} entities, {covered}/{total} exact transitions; "
          f"{len(model['flows'])} ordered scenarios, {len(model.get('edge_examples', {}))} isolated examples; wrote docs/05-flow-map.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
