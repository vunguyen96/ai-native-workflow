#!/usr/bin/env python3
"""Show where a work item stands. READ-ONLY: it never changes any file.

It reads tickets/<ID>/run-<N>/manifest.yaml and config/lanes.yaml, then derives:
  - the effective status of every artifact, including staleness (from recorded hashes),
  - the state of every lane: waiting, ready, working, done, blocked, stale, failed,
  - the ticket status, and the next actions (who or what moves the ticket forward).

Usage:
  python scripts/wf_status.py PROJ-123              # latest run
  python scripts/wf_status.py PROJ-123 --run 2
  python scripts/wf_status.py --all                 # one line per work item
  python scripts/wf_status.py PROJ-123 --json
  python scripts/wf_status.py --hash tickets/PROJ-123/run-1/artifacts/spec.md

--hash prints the short hash to record in the manifest (`hash:` of an artifact, or its `inputs:`).
Quote it in YAML, for example hash: "0123abcd", so it is never read as a number.

Until the orchestrator exists, the manifest is edited by hand: this script reads it, it does not write it.
Needs PyYAML: pip install -r requirements.txt (the other scripts use only the standard library).
"""
import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.dont_write_bytecode = True

try:
    import yaml
except ImportError:
    sys.exit("error: wf_status.py needs PyYAML. Install it with: python -m pip install -r requirements.txt\n(the other scripts use only the standard library)")

ROOT = Path(__file__).resolve().parent.parent
OK = ("ready", "approved")
ART_STATUSES = ("missing", "draft", "ready", "approved", "rejected", "stale")
PROPOSALS = ("question_raised", "spec_gap", "verify_failed", "review_rejected", "escalate")


class StatusError(Exception):
    pass


# ---------------------------------------------------------------- reading

def load_yaml(path):
    # A worker may be replacing the manifest right now (it writes atomically, but Windows can briefly refuse
    # a read), so retry a few times before reporting a problem.
    for attempt in range(6):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            break
        except (OSError, yaml.YAMLError) as e:
            if attempt == 5:
                raise StatusError(f"cannot read {path.name}: {e}")
            time.sleep(0.1)
    return data if isinstance(data, dict) else {}


def latest_run(ticket_dir):
    runs = [int(m.group(1)) for p in ticket_dir.glob("run-*")
            if p.is_dir() and (m := re.fullmatch(r"run-(\d+)", p.name))]
    return max(runs) if runs else None


def sha256_path(p):
    h = hashlib.sha256()
    if p.is_dir():
        for f in sorted(x for x in p.rglob("*") if x.is_file()):
            h.update(f.relative_to(p).as_posix().encode())
            h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    else:
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def same_hash(a, b):
    a, b = str(a).lower(), str(b).lower()
    n = min(len(a), len(b))
    return n > 0 and a[:n] == b[:n]


def artifact_path(run_dir, name):
    for cand in (run_dir / "artifacts" / name, run_dir / name):
        if cand.exists():
            return cand
    return None


def read_events(path):
    events = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t", 3)
        if len(parts) < 3:
            continue
        detail = {}
        if len(parts) == 4:
            try:
                detail = json.loads(parts[3])
            except json.JSONDecodeError:
                detail = {"raw": parts[3]}
        events.append({"time": parts[0], "actor": parts[1], "event": parts[2], "detail": detail})
    return events


def proposal_id(e):
    """Stable id of a proposed event, used by the orchestrator side to record that it applied it."""
    return hashlib.sha1((e["time"] + e["actor"] + e["event"] + json.dumps(e["detail"], sort_keys=True)).encode()).hexdigest()[:10]


def telemetry(run_dir, events):
    out = {}
    for e in events:
        if e["event"] != "lane_started":
            continue
        d = e["detail"]
        lane, repo, att = d.get("lane"), d.get("repo") or "", d.get("attempt") or 1
        name = "-".join(p for p in (lane, repo) if p) + f"-a{att}.json"
        status = "not checked"
        mp = run_dir / "metrics" / name
        if mp.exists():
            try:
                status = json.loads(mp.read_text(encoding="utf-8")).get("otel_status", "unknown")
            except (json.JSONDecodeError, OSError):
                status = "unreadable"
        out.setdefault(lane, []).append({"repo": repo, "attempt": att, "otel": status})
    return out


# ---------------------------------------------------------------- contracts

def split_token(token):
    name, _, level = str(token).partition("@")
    return name.strip().rstrip("/"), (level.strip() or None)


def artifact_key(name, known):
    if name in known:
        return name
    first = name.split("/")[0]
    return first if first in known else None


def lane_contract(cfg, kind, lane, known):
    ldef = (cfg.get("lanes") or {}).get(lane) or {}
    kdef = (cfg.get("kinds") or {}).get(kind) or {}
    produces = ldef.get("produces") or []
    if lane == "planner" and kdef.get("planner_produces"):
        produces = kdef["planner_produces"]
    pk = []
    for t in produces:
        key = artifact_key(split_token(t)[0], known)
        if key and key not in pk:
            pk.append(key)
    req = []
    for t in ldef.get("requires") or []:
        name, level = split_token(t)
        key = artifact_key(name, known)
        if key and (key, level) not in req:
            req.append((key, level))
    gate = kdef.get("gate") if (lane == "planner" and kdef.get("gate")) else ldef.get("gate")
    return pk, req, gate, ldef


# ---------------------------------------------------------------- artifacts

def stale_reason(run_dir, arts, entry, status, own_name):
    if status not in OK:
        return None
    for inp, rec in (entry.get("inputs") or {}).items():
        cur = None
        p = artifact_path(run_dir, str(inp))
        if p is not None:
            cur = sha256_path(p)
        else:
            other = arts.get(inp)
            if isinstance(other, dict) and other.get("hash"):
                cur = str(other["hash"])
        if cur and not same_hash(cur, rec):
            return f"input {inp} changed (recorded {str(rec)[:8]}, now {cur[:8]})"
    own = entry.get("hash")
    if own:
        p = artifact_path(run_dir, own_name)
        if p is not None and not same_hash(sha256_path(p), own):
            return f"{own_name} was edited after its hash was recorded"
    return None


def aggregate_changes(v):
    sts = [r["status"] for r in v["repos"].values()]
    if not sts:
        return
    if any(s == "stale" for s in sts):
        v["status"] = "stale"
    elif any(s == "rejected" for s in sts):
        v["status"] = "rejected"
    elif all(s == "approved" for s in sts):
        v["status"] = "approved"
    elif all(s in OK for s in sts):
        v["status"] = "ready"
    elif any(s == "draft" or s in OK for s in sts):
        v["status"] = "draft"
    else:
        v["status"] = "missing"


def build_view(run_dir, manifest, cfg, kind, lane_names, warnings):
    arts = manifest.get("artifacts") or {}
    view = {}
    for name, a in arts.items():
        a = a if isinstance(a, dict) else {}
        st = str(a.get("status", "missing"))
        if st not in ART_STATUSES:
            warnings.append(f"artifact {name}: unknown status {st!r} (treated as missing)")
            st = "missing"
        recorded = st
        path = artifact_path(run_dir, str(name))
        if (
            st == "missing"
            and name != "ticket-context.md"
            and path is not None
            and path.is_file()
            and path.stat().st_size > 0
        ):
            st = "draft"
        view[name] = {"status": st, "recorded": recorded, "reason": None, "entry": a, "repos": {}}

    ch = view.get("changes")
    if ch:
        per = ch["entry"].get("per_repo") or {}
        for repo, e in per.items():
            e = e if isinstance(e, dict) else {"status": e}
            st = str(e.get("status", "missing"))
            if st not in ART_STATUSES:
                warnings.append(f"changes/{repo}: unknown status {st!r} (treated as missing)")
                st = "missing"
            ch["repos"][str(repo)] = {"status": st, "recorded": st, "reason": None, "entry": e}

    # staleness from recorded hashes
    for name, v in view.items():
        if name == "changes":
            for repo, r in v["repos"].items():
                reason = stale_reason(run_dir, arts, r["entry"], r["status"], f"changes/{repo}")
                if reason:
                    r["status"], r["reason"] = "stale", reason
            aggregate_changes(v)
            continue
        reason = stale_reason(run_dir, arts, v["entry"], v["status"], name)
        if reason:
            v["status"], v["reason"] = "stale", reason

    # staleness propagates down the lane contracts
    deps = {}
    for lane in lane_names:
        pk, req, _, _ = lane_contract(cfg, kind, lane, set(view))
        for p in pk:
            deps.setdefault(p, set()).update(k for k, _ in req)
    for _ in range(len(view) + 1):
        changed = False
        for p, ds in deps.items():
            v = view.get(p)
            if v is None:
                continue
            src = next((d for d in sorted(ds) if d in view and d != p and view[d]["status"] == "stale"), None)
            if src is None:
                continue
            why = f"built on {src}, which is stale"
            if p == "changes" and v["repos"]:
                for r in v["repos"].values():
                    if r["status"] in OK:
                        r["status"], r["reason"] = "stale", why
                        changed = True
                aggregate_changes(v)
            elif v["status"] in OK:
                v["status"], v["reason"] = "stale", why
                changed = True
        if not changed:
            break

    # sanity checks on what a human wrote
    for name, v in view.items():
        if name == "changes":
            for repo, r in v["repos"].items():
                if r["recorded"] in OK and artifact_path(run_dir, f"changes/{repo}") is None:
                    warnings.append(f"changes/{repo} is marked {r['recorded']} but no folder was found under artifacts/")
            continue
        if v["recorded"] in OK and artifact_path(run_dir, name) is None:
            warnings.append(f"{name} is marked {v['recorded']} but no file was found under artifacts/")
        if v["recorded"] == "approved" and not v["entry"].get("approved_by"):
            warnings.append(f"{name} is approved but has no approved_by")
    return view


# ---------------------------------------------------------------- lanes and gates

def unmet_requirements(view, req, dev_repos):
    unmet = []
    for key, level in req:
        if key == "changes":
            if not dev_repos:
                unmet.append("changes (no repos listed in the manifest yet)")
                continue
            bad = [r for r in dev_repos if (view["changes"]["repos"].get(r) or {}).get("status") not in OK]
            if bad:
                unmet.append("changes from " + ", ".join(bad))
            continue
        need = ("approved",) if level == "approved" else OK
        if view[key]["status"] not in need:
            label = f"{key}@approved" if level == "approved" else key
            unmet.append(f"{label} (is {view[key]['status']})")
    return unmet


def is_accepted(kind, view, manifest):
    if kind == "spike":
        acc = manifest.get("acceptance") or {}
        return (view.get("findings.md") or {}).get("status") == "approved" or acc.get("accepted") is True
    return (manifest.get("acceptance") or {}).get("ticket_accepted") is True


def gate_state(lane, gate, view, manifest):
    if not gate:
        return None
    keys = [k for k in (artifact_key(split_token(t)[0], set(view)) for t in gate.get("artifacts") or []) if k]
    if not keys:
        return None
    sts = [view[k]["status"] for k in keys]
    if all(s == "approved" for s in sts):
        return {"state": "approved", "artifacts": keys}
    if any(s == "rejected" for s in sts):
        return {"state": "rejected", "artifacts": keys}
    if all(s in OK for s in sts):
        if lane == "reviewer":
            human = (manifest.get("review") or {}).get("human_required")
            if human is False:
                return {"state": "not_required", "artifacts": keys}
            if human is None:
                return {"state": "needs_risk_decision", "artifacts": keys}
        return {"state": "awaiting_approval", "artifacts": keys}
    return {"state": "open", "artifacts": keys}


def compute_lanes(cfg, kind, lane_names, view, manifest, dev_repos, tele, warnings):
    mlanes = manifest.get("lanes") or {}
    accepted = is_accepted(kind, view, manifest)
    lanes = {}
    for lane in lane_names:
        pk, req, _, ldef = lane_contract(cfg, kind, lane, set(view))
        ment = mlanes.get(lane) if isinstance(mlanes.get(lane), dict) else {}
        mstat = str(ment.get("status", ""))
        unmet = unmet_requirements(view, req, dev_repos)
        trigger = ldef.get("trigger")
        if trigger == "ticket_accepted" and not accepted:
            unmet.append("ticket accepted (acceptance policy is TBD)")
        rec = {"state": "waiting", "note": "", "unmet": unmet, "rework": False, "repos": {}, "launch": []}

        if ment.get("enabled") is False:
            rec.update(state="disabled", note="disabled in the manifest (set enabled: true to run it)")
        elif lane == "dev":
            compute_dev(rec, view, ment, unmet, dev_repos)
        else:
            sts = [view[k]["status"] for k in pk]
            rework = any(s == "rejected" for s in sts)
            if mstat == "failed":
                rec["state"] = "failed"
            elif mstat == "blocked":
                rec["state"] = "blocked"
            elif rework:
                rec["state"], rec["rework"] = ("waiting" if unmet else "ready"), True
            elif pk and any(s == "stale" for s in sts):
                rec["state"] = "stale"
            elif pk and all(s in OK for s in sts):
                rec["state"] = "done"
            elif mstat == "working" or any(s == "draft" or s in OK for s in sts):
                rec["state"] = "working"
            else:
                rec["state"] = "waiting" if unmet else "ready"
            if rec["state"] == "stale" and unmet:
                rec["state"] = "stale"
            why = [view[k]["reason"] for k in pk if view[k]["reason"]]
            rec["note"] = lane_note(rec, unmet, why, rework)
            if mstat == "done" and rec["state"] != "done":
                warnings.append(f"manifest says lane {lane} is done, but its artifacts say {rec['state']}")
        if lane in tele:
            c = Counter(t["otel"] for t in tele[lane])
            extra = ", ".join(f"{n} {k}" for k, n in sorted(c.items()))
            rec["note"] = (rec["note"] + "  " if rec["note"] else "") + f"[sessions: {len(tele[lane])}; otel: {extra}]"
        rec["resumable"] = bool(ment.get("retry_after_recheck")) and rec["state"] in ("ready", "stale", "working")
        lanes[lane] = rec
    return lanes


def lane_note(rec, unmet, why, rework):
    s = rec["state"]
    if s == "waiting":
        return "needs: " + "; ".join(unmet)
    if s == "ready":
        return "rework: an artifact was rejected" if rework else "can start"
    if s == "stale":
        return "; ".join(why) if why else "an input changed"
    if s == "working":
        return "has draft or partial artifacts"
    if s == "blocked":
        return "blocked (per manifest), waiting for an answer or a dependency"
    if s == "failed":
        return "failed (per manifest), escalate to a human"
    return ""


def compute_dev(rec, view, ment, unmet, dev_repos):
    mrepos = ment.get("repos") if isinstance(ment.get("repos"), dict) else {}
    if not dev_repos:
        rec["state"] = "waiting" if unmet else "ready"
        rec["note"] = ("needs: " + "; ".join(unmet)) if unmet else "ready, but the manifest lists no repos yet (add them under lanes.dev.repos)"
        return
    for r in dev_repos:
        rv = view["changes"]["repos"].get(r) or {"status": "missing", "reason": None}
        ms, s = str(mrepos.get(r, "")), rv["status"]
        rework = False
        if ms in ("failed", "blocked"):
            st = ms
        elif s == "rejected":
            st, rework = ("waiting" if unmet else "ready"), True
        elif s == "stale":
            st = "stale"
        elif s in OK:
            st = "done"
        elif s == "draft" or ms == "working":
            st = "working"
        else:
            st = "waiting" if unmet else "ready"
        note = {"waiting": "needs: " + "; ".join(unmet), "ready": "rework: changes were rejected" if rework else "can start",
                "stale": rv["reason"] or "an input changed", "failed": "failed (per manifest)",
                "blocked": "blocked (per manifest)"}.get(st, "")
        rec["repos"][r] = {"state": st, "note": note, "rework": rework}
    states = [x["state"] for x in rec["repos"].values()]
    if "failed" in states:
        rec["state"] = "failed"
    elif "blocked" in states:
        rec["state"] = "blocked"
    elif "stale" in states:
        rec["state"] = "stale"
    elif all(s == "done" for s in states):
        rec["state"] = "done"
    elif any(s in ("working", "done") for s in states):
        rec["state"] = "working"
    elif "ready" in states:
        rec["state"] = "ready"
    else:
        rec["state"] = "waiting"
    rec["note"] = ", ".join(f"{r}: {x['state']}" for r, x in rec["repos"].items())
    if rec["state"] == "waiting" and unmet:
        rec["note"] = "needs: " + "; ".join(unmet)


# ---------------------------------------------------------------- ticket status

def derive_ticket(kind, lanes, gates, view, manifest, dev_repos, relrepos):
    def st(l):
        return (lanes.get(l) or {}).get("state")

    failed = [l for l, v in lanes.items() if v["state"] == "failed"]
    rfail = [r for r, v in relrepos.items() if v == "failed"]
    if failed or rfail:
        bits = []
        if failed:
            bits.append("lane failed: " + ", ".join(failed))
        if rfail:
            bits.append("release failed in " + ", ".join(rfail) + " (partial release: stop, record what is merged or deployed)")
        return "Escalated", "; ".join(bits)
    blocked = [l for l, v in lanes.items() if v["state"] == "blocked"]
    if blocked:
        return "Needs input", ", ".join(blocked) + " blocked on a question or dependency"
    if is_accepted(kind, view, manifest):
        return "Done", "accepted" if kind != "spike" else "findings accepted"

    pg = (gates.get("planner") or {}).get("state")
    if kind == "spike":
        if st("planner") == "done" and pg == "awaiting_approval":
            return "Awaiting findings review", "findings.md is ready; a human accepts, rejects or asks for more"
        return "Investigating", f"planner is {st('planner')}"

    if st("planner") != "done" or pg != "approved":
        p = lanes.get("planner") or {}
        if st("planner") == "done" and pg == "awaiting_approval":
            return "Planning", "spec, design and tasks are ready and await human approval"
        if p.get("rework") or pg == "rejected":
            return "Planning", "rework: the plan was rejected"
        if st("planner") == "stale":
            return "Planning", "the plan is stale: " + (p.get("note") or "an input changed")
        return "Planning", f"planner is {st('planner')}"
    if st("qe") != "done" or st("dev") != "done":
        parts = [f"qe {st('qe')}"]
        dev = lanes.get("dev") or {}
        parts.append("dev " + (dev.get("note") or st("dev")))
        return "Building", "spec approved; " + "; ".join(parts)
    if view["verify-report.md"]["status"] not in OK:
        return "Verifying", f"all changes and the test plan are in; verify-report is {view['verify-report.md']['status']}"
    rg = (gates.get("reviewer") or {}).get("state")
    if rg not in ("approved", "not_required"):
        why = {"awaiting_approval": "review-report is ready and needs a human (risk-flagged)",
               "needs_risk_decision": "review-report is ready; the risk decision is missing (set review.human_required)",
               "rejected": "review was rejected: rework"}.get(rg, f"review-report is {view['review-report.md']['status']}")
        return "In review", why
    if not dev_repos or any(relrepos.get(r) != "deployed" for r in dev_repos):
        done = [r for r in dev_repos if relrepos.get(r) == "deployed"]
        return "Deploying", f"review passed; deployed {len(done)} of {len(dev_repos)} repos"
    return "Awaiting acceptance", "deployed; acceptance policy is TBD (set acceptance.ticket_accepted when decided)"


# ---------------------------------------------------------------- next actions

def next_actions(ticket, run, kind, lanes, gates, view, manifest, dev_repos, relrepos, tele, status, tasks_hint):
    acts = []

    def launch(lane, repo=None):
        n = 1 + sum(1 for t in tele.get(lane, []) if (t["repo"] or None) == repo)
        cmd = f"python scripts/wf_launch.py --ticket {ticket} --run {run} --lane {lane}"
        if repo:
            cmd += f" --repo {repo}"
        return cmd + f" --attempt {n} -- <your copilot command>"

    if view["ticket-context.md"]["status"] not in OK:
        acts.append(("human", f"Fill artifacts/ticket-context.md, then: python scripts/wf_run.py ready --ticket {ticket} (it is {view['ticket-context.md']['status']})"))
    for lane, rec in lanes.items():
        s = rec["state"]
        if rec.get("resumable"):
            acts.append(("agent", f"Resume {lane}: python scripts/wf_run.py worker --ticket {ticket} --run {run} --lane {lane}"))
        if lane == "dev" and rec["repos"]:
            for r, x in rec["repos"].items():
                if x["state"] in ("ready", "stale") and not rec["unmet"]:
                    acts.append(("agent", ("Re-run " if x["state"] == "stale" else "Run ") + f"dev for {r}: " + launch("dev", r)))
        elif lane != "dev" and s in ("ready", "stale") and not rec["unmet"]:
            verb = "Re-run" if s == "stale" else "Run"
            acts.append(("agent", f"{verb} {lane}: " + launch(lane)))
    dev = lanes.get("dev") or {}
    if dev.get("state") == "ready" and not dev.get("repos"):
        hint = f" (tasks.md mentions: {', '.join(tasks_hint)})" if tasks_hint else ""
        acts.append(("human", "List the repos from tasks.md in the manifest under lanes.dev.repos and artifacts.changes.per_repo" + hint))

    pg = (gates.get("planner") or {}).get("state")
    if pg == "awaiting_approval":
        names = ", ".join((gates["planner"])["artifacts"])
        what = "Review the findings" if kind == "spike" else "Review the plan, including the traceability table,"
        acts.append(("human", f"{what} ({names}), then: python scripts/wf_run.py approve --ticket {ticket} --gate planner   (or reject --reason ...)"))
    rg = (gates.get("reviewer") or {}).get("state")
    if rg == "awaiting_approval":
        acts.append(("human", f"Review the change (risk-flagged), then: python scripts/wf_run.py approve --ticket {ticket} --gate reviewer   (or reject --reason ...)"))
    if rg == "needs_risk_decision":
        acts.append(("human", "Evaluate config/risk-rules.yaml and set review.human_required to true or false in manifest.yaml"))

    for lane, rec in lanes.items():
        if rec["state"] == "blocked":
            acts.append(("human", f"{lane} is blocked on a question (see question_raised in events.log). Answer it, then: python scripts/wf_run.py answer --ticket {ticket} --lane {lane} --text \"...\""))

    if status == "Deploying":
        order = [str(x) for x in ((manifest.get("release") or {}).get("sequence") or [])]
        mode = (manifest.get("release") or {}).get("deploy_order", "independent")
        todo = [r for r in (order if mode == "ordered" and order else dev_repos) if relrepos.get(r) != "deployed"]
        if todo:
            tail = f" (ordered: {' -> '.join(order)})" if mode == "ordered" and order else ""
            nxt = todo[0] if mode == "ordered" else ", ".join(todo)
            acts.append(("ci/cd", f"Merge and deploy {nxt}{tail}; record it: python scripts/wf_run.py deployed --ticket {ticket} --repo <name>"))
    if status == "Awaiting acceptance":
        acts.append(("human", f"Decide acceptance (policy is TBD), then: python scripts/wf_run.py accept --ticket {ticket}"))

    for lane, runs in tele.items():
        unchecked = [t for t in runs if t["otel"] == "not checked"]
        if unchecked:
            acts.append(("agent", f"Check telemetry for {lane}: python scripts/otel_check.py --ticket {ticket} --run {run} --lane {lane}"))
    if not acts and status != "Done":
        acts.append(("system", "Nothing can start yet. The Lanes table says what each waiting lane needs"))
    return acts


# ---------------------------------------------------------------- analysis

def body_of(path):
    """File text without the artifact header (the --- block at the top)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2]
    return text


def shape_warnings(run_dir, manifest, view, registry, warnings):
    """Warn when a machine-read part of an artifact is missing. Read-only; templates are in skills/<lane>/templates/."""
    def got(name):
        v = view.get(name)
        if not v or v["recorded"] == "missing":
            return None
        p = artifact_path(run_dir, name)
        return body_of(p) if p is not None and p.is_file() else None

    spec = got("spec.md")
    ac_ids = []
    if spec is not None:
        ac_ids = sorted(set(re.findall(r"^\|\s*(AC-\d+)\s*\|", spec, re.M)), key=lambda a: int(a[3:]))
        if not ac_ids:
            warnings.append("spec.md: no acceptance criteria found (expected a table with ids AC-1, AC-2, ...)")
        if not re.search(r"^#+\s*Traceability\b", spec, re.M | re.I):
            warnings.append("spec.md: no Traceability section")
    tasks = got("tasks.md")
    if tasks is not None:
        m = re.search(r"^deploy_order:\s*(\w+)", tasks, re.M)
        if not m or m.group(1) not in ("independent", "ordered"):
            warnings.append("tasks.md: the Release section needs a line 'deploy_order: independent' or 'deploy_order: ordered'")
        elif m.group(1) == "ordered":
            sq = re.search(r"^sequence:\s*(.+)$", tasks, re.M)
            seq = [x.strip() for x in sq.group(1).split(",") if x.strip()] if sq else []
            if not seq:
                warnings.append("tasks.md: deploy_order is ordered but there is no 'sequence:' line")
            elif registry:
                unknown = [r for r in seq if r not in registry]
                if unknown:
                    warnings.append("tasks.md: sequence names repos that are not in config/repos.yaml: " + ", ".join(unknown))
    plan = got("test-plan.md")
    if plan is not None and ac_ids:
        gaps = [a for a in ac_ids if not re.search(re.escape(a) + r"(?!\d)", plan)]
        if gaps:
            warnings.append("test-plan.md: no row for " + ", ".join(gaps))
    ver = got("verify-report.md")
    if ver is not None and not re.search(r"^Result:\s*\**\s*(PASS|FAIL)", ver, re.M | re.I):
        warnings.append("verify-report.md: no 'Result: PASS' or 'Result: FAIL' line")
    rev = got("review-report.md")
    if rev is not None:
        head = [l.strip() for l in rev.splitlines() if l.strip()][:4]
        hm = next((re.fullmatch(r"human_required:\s*(true|false)", l) for l in head if l.startswith("human_required")), None)
        if not hm:
            warnings.append("review-report.md: the first lines must include 'human_required: true' or 'human_required: false'")
        else:
            decided = (manifest.get("review") or {}).get("human_required")
            if isinstance(decided, bool) and decided != (hm.group(1) == "true"):
                warnings.append(f"review.human_required in the manifest ({str(decided).lower()}) differs from review-report.md ({hm.group(1)})")
        if not re.search(r"^Verdict:\s*(pass|rework|design_rejected)\b", rev, re.M | re.I):
            warnings.append("review-report.md: no 'Verdict: pass|rework|design_rejected' line")


def analyze(ticket, run=None):
    tdir = ROOT / "tickets" / ticket
    if not tdir.is_dir():
        raise StatusError(f"no such work item: tickets/{ticket} (create it with scripts/new_ticket.py)")
    if run is None:
        run = latest_run(tdir)
        if run is None:
            raise StatusError(f"{ticket} has no run-N folder")
    run_dir = tdir / f"run-{run}"
    if not (run_dir / "manifest.yaml").exists():
        raise StatusError(f"tickets/{ticket}/run-{run}/manifest.yaml not found")
    manifest = load_yaml(run_dir / "manifest.yaml")
    cfg_path = ROOT / "config" / "lanes.yaml"
    if not cfg_path.exists():
        raise StatusError("config/lanes.yaml not found")
    cfg = load_yaml(cfg_path)

    kind = str(manifest.get("kind") or "story")
    kinds = cfg.get("kinds") or {}
    if kind not in kinds:
        raise StatusError(f"unknown kind {kind!r} (config/lanes.yaml has: {', '.join(kinds)})")
    lane_names = list(kinds[kind].get("lanes") or [])
    warnings = []

    view = build_view(run_dir, manifest, cfg, kind, lane_names, warnings)
    for need in ("ticket-context.md",):
        if need not in view:
            raise StatusError(f"manifest has no artifact {need}")
    if kind != "spike":
        for need in ("verify-report.md", "review-report.md"):
            if need not in view:
                raise StatusError(f"manifest has no artifact {need}")

    mlanes = manifest.get("lanes") or {}
    dev_repos = []
    if "changes" in view:
        dev_repos = list(view["changes"]["repos"])
    drepos = (mlanes.get("dev") or {}).get("repos") if isinstance(mlanes.get("dev"), dict) else None
    for r in (drepos or {}):
        if str(r) not in dev_repos:
            dev_repos.append(str(r))
    if "changes" in view:
        for r in dev_repos:
            view["changes"]["repos"].setdefault(r, {"status": "missing", "recorded": "missing", "reason": None, "entry": {}})

    events = read_events(run_dir / "events.log")
    tele = telemetry(run_dir, events)
    lanes = compute_lanes(cfg, kind, lane_names, view, manifest, dev_repos, tele, warnings)
    gates = {}
    for lane in ("planner", "reviewer"):
        if lane in lanes:
            _, _, gate, _ = lane_contract(cfg, kind, lane, set(view))
            g = gate_state(lane, gate, view, manifest)
            if g:
                gates[lane] = g

    rel = manifest.get("release") if isinstance(manifest.get("release"), dict) else {}
    relrepos = {str(k): str(v) for k, v in (rel.get("repos") or {}).items()}
    status, detail = derive_ticket(kind, lanes, gates, view, manifest, dev_repos, relrepos)

    registry = []
    rp = ROOT / "config" / "repos.yaml"
    if rp.exists():
        registry = list((load_yaml(rp).get("repos") or {}))
    for r in dev_repos:
        if registry and r not in registry:
            warnings.append(f"repo {r} is not in config/repos.yaml")
    shape_warnings(run_dir, manifest, view, registry, warnings)
    tasks_hint = []
    tp = artifact_path(run_dir, "tasks.md")
    if tp is not None and registry and tp.is_file():
        text = tp.read_text(encoding="utf-8", errors="replace")
        tasks_hint = [n for n in registry if re.search(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])", text)]

    actions = next_actions(ticket, run, kind, lanes, gates, view, manifest, dev_repos, relrepos, tele, status, tasks_hint)
    applied = {e["detail"].get("id") for e in events if e["event"] == "proposal_applied"}
    proposals = [e for e in events if e["event"] in PROPOSALS and proposal_id(e) not in applied][-5:]

    arts_out = {}
    for name, v in view.items():
        item = {"status": v["status"], "recorded": v["recorded"], "reason": v["reason"]}
        if name == "changes":
            item["repos"] = {r: {"status": x["status"], "recorded": x["recorded"], "reason": x["reason"]} for r, x in v["repos"].items()}
        arts_out[name] = item
    return {"ticket": ticket, "run": run, "kind": kind, "path": run_dir.relative_to(ROOT).as_posix(),
            "status": status, "detail": detail, "lanes": lanes, "gates": gates, "artifacts": arts_out,
            "release": {"deploy_order": str(rel.get("deploy_order", "independent")),
                        "sequence": [str(x) for x in rel.get("sequence") or []], "repos": relrepos},
            "next": [{"who": w, "text": t} for w, t in actions],
            "proposals": [{"time": e["time"], "actor": e["actor"], "event": e["event"], "detail": e["detail"]} for e in proposals],
            "warnings": warnings}


# ---------------------------------------------------------------- output

def render(res):
    L = [f"{res['ticket']}  run {res['run']}  {res['kind']}   ({res['path']})",
         f"Status: {res['status'].upper()}  -  {res['detail']}", "",
         "Lanes  (derived from artifacts; the manifest's lane status only counts for working, blocked and failed)"]
    for name, ln in res["lanes"].items():
        L.append(f"  {name:<14} {ln['state']:<9} {ln['note']}".rstrip())
        for repo, x in ln["repos"].items():
            L.append(f"    {repo:<12} {x['state']:<9} {x['note']}".rstrip())
    L += ["", "Artifacts"]
    for name, a in res["artifacts"].items():
        if name == "changes" and a.get("repos"):
            for repo, x in a["repos"].items():
                L.append(f"  {'changes/' + repo:<22} {_art(x)}")
        else:
            L.append(f"  {name:<22} {_art(a)}")
    L += ["", "Next"]
    for a in res["next"]:
        L.append(f"  [{a['who']}] {a['text']}")
    if res["proposals"]:
        L += ["", "Proposed events (nothing applies them yet: act on them by hand, then record the outcome in decisions.md)"]
        for p in res["proposals"]:
            d = json.dumps(p["detail"], sort_keys=True)
            L.append(f"  {p['time']}  {p['actor']}  {p['event']}  {d[:100]}")
    if res["warnings"]:
        L += ["", "Warnings"]
        L += [f"  - {w}" for w in res["warnings"]]
    return "\n".join(L)


def _art(a):
    s = a["status"]
    if a["recorded"] != s:
        s += f"  (manifest says {a['recorded']})"
    if a["reason"]:
        s += f"  - {a['reason']}"
    return s


def list_all():
    base = ROOT / "tickets"
    rows = []
    for d in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("_")) if base.is_dir() else []:
        try:
            r = analyze(d.name)
            rows.append((r["ticket"], r["kind"], r["run"], r["status"], r["detail"], len(r["warnings"])))
        except StatusError as e:
            rows.append((d.name, "?", "?", "Error", str(e), 0))
    if not rows:
        print("no work items yet (create one with scripts/new_ticket.py)")
        return
    for t, k, n, s, d, w in rows:
        print(f"{t:<14} {k:<6} run {n!s:<3} {s:<24} {d}" + (f"  [{w} warning(s)]" if w else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticket", nargs="?", help="work item id, for example PROJ-123")
    ap.add_argument("--ticket", dest="ticket_opt", help="same as the positional argument")
    ap.add_argument("--run", type=int, default=None, help="default: the latest run")
    ap.add_argument("--all", action="store_true", help="one line per work item")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--hash", metavar="PATH", help="print the short hash of a file or folder")
    args = ap.parse_args()

    if args.hash:
        p = Path(args.hash)
        if not p.exists():
            sys.exit(f"error: {p} not found")
        print(sha256_path(p)[:8])
        print("quote it in YAML, for example hash: \"" + sha256_path(p)[:8] + "\"", file=sys.stderr)
        return
    if args.all:
        list_all()
        return
    ticket = args.ticket or args.ticket_opt
    if not ticket:
        ap.error("give a work item id, or use --all")
    try:
        res = analyze(ticket, args.run)
    except StatusError as e:
        sys.exit(f"error: {e}")
    print(json.dumps(res, indent=2, default=str) if args.json else render(res))


if __name__ == "__main__":
    main()
