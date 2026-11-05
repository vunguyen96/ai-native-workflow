#!/usr/bin/env python3
"""Lane workers: start a run once, then every lane works on its own.

Each lane is a small loop that looks at the derived state (scripts/wf_status.py). When its lane is ready
it takes a lease, launches one agent session through scripts/wf_launch.py, checks the telemetry, records
the artifacts it produced in the manifest, and applies the events that agents proposed. There is no
central controller: workers only share the files in the run folder.

Commands (run from the project root):
  start    --ticket T [--run N] [--auto-approve] [--verbose]   start one worker per lane and wait
  worker   --ticket T --lane L                                 run one lane's loop by hand (what start spawns)
  ready    --ticket T [--artifact ticket-context.md]           intake: mark the ticket context ready
  recheck  --ticket T                                          sync artifact frontmatter status to the manifest
  approve  --ticket T [--gate planner|reviewer] [--by NAME]    human gate: approve and move on
  reject   --ticket T --gate planner|reviewer --reason TEXT    human gate: send the work back
  answer   --ticket T --lane L [--text TEXT]                   unblock a lane after a question_raised
  enable   --ticket T --lane L                                 turn on a lane that is off by default (Inspect & Adapt on a spike)
  accept   --ticket T [--by NAME]                              human: the ticket is accepted
  deployed --ticket T --repo R                                 record a deploy done outside (CI/CD)
  command  --ticket T --lane L [--repo R]                      print the agent command a worker would run
  status   --ticket T                                          same as wf_status.py

--auto-approve plays the human at every gate. It is for testing the flow only.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import wf_status as S  # noqa: E402
import wf_state as W  # noqa: E402

try:
    import yaml
except ImportError:
    sys.exit("error: PyYAML is required. Install it with: python -m pip install -r requirements.txt")

ROOT = S.ROOT
PY = sys.executable
OK = S.OK
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

PLACEHOLDER = re.compile(r"<(paste|the question|text|link to|ids|repos|what will|why this|for example|tech-debt)", re.I)


def say(who, msg):
    print(f"[{who}] {msg}", flush=True)


def load_cfg(name, default=None):
    p = ROOT / "config" / name
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else (default or {})


# ---------------------------------------------------------------- what to launch

def agent_command(runner, run, lane, repo, attempt):
    fmt = {"lane": lane, "repo": repo, "ticket": run.ticket, "run": run.run, "attempt": attempt,
           "root": str(ROOT), "run_dir": str(run.dir), "repo_phrase": f" for repo {repo}" if repo else "",
           "repo_line": f"\n  repo: {repo} (your working directory)" if repo else ""}
    prompt = str(runner.get("prompt", "")).format(**fmt)
    fmt2 = dict(fmt, prompt=prompt, repo="-" + repo if repo else "")
    override = os.environ.get("WF_AGENT_CMD")
    base = json.loads(override) if override else list((runner.get("agent") or {}).get("command") or [])
    if not base:
        sys.exit("error: no agent command. Set agent.command in config/runner.yaml or WF_AGENT_CMD")
    extra = list(((runner.get("agent") or {}).get("by_lane") or {}).get(lane) or [])
    return [str(x).format(**fmt2) for x in base + extra]


def launchable(res, lane, run, max_attempts):
    """Lane instances (repo names, or '' for the lane itself) that may start now."""
    rec = (res["lanes"] or {}).get(lane)
    if not rec or rec["unmet"] or res["status"] == "Escalated":
        return []
    units = []
    if lane == "dev":
        for repo, x in rec["repos"].items():
            if x["state"] in ("ready", "stale") or (x["state"] == "working" and run.last_outcome(lane, repo) in ("incomplete", "crashed")):
                units.append(repo)
    elif rec["state"] in ("ready", "stale") or (rec["state"] == "working" and run.last_outcome(lane, "") in ("incomplete", "crashed")):
        units.append("")
    return units


# ---------------------------------------------------------------- after a session ends

def apply_proposals(run, who):
    """Apply pending agent proposals exactly once. Any worker may do it; the log says which ones are done."""
    events = run.events()
    done = {e["detail"].get("id") for e in events if e["event"] == "proposal_applied"}
    repos = list(((run.load().get("lanes") or {}).get("dev") or {}).get("repos") or {})
    for e in events:
        if e["event"] not in S.PROPOSALS or e["actor"] in ("orchestrator", "launcher"):
            continue
        pid = S.proposal_id(e)
        if pid in done:
            continue
        d = e["detail"]
        name = e["event"]
        lane = d.get("lane") or next((l for l in ("planner", "qe", "dev", "reviewer", "inspect-adapt") if l in e["actor"]), None)
        if name == "verify_failed":
            targets = [d["repo"]] if d.get("repo") else repos
            for r in targets:
                run.mark("changes", "rejected", repo=r)
                run.event("orchestrator", "rework_requested", {"repo": r, "acs": d.get("acs"), "lane": "dev"})
            run.decide(f"verify_failed accepted: changes/{', '.join(targets)} rejected ({', '.join(d.get('acs') or [])}). Only that repo is redone.")
            say(who, f"applied verify_failed: only {', '.join(targets)} goes back to dev")
        elif name == "review_rejected":
            run.mark("review-report.md", "rejected")
            rs = d.get("repo") or d.get("repos") or []
            for r in ([rs] if isinstance(rs, str) else rs):
                run.mark("changes", "rejected", repo=r)
            run.decide(f"review_rejected accepted: {d.get('reason', 'no reason given')}")
            say(who, "applied review_rejected")
        elif name == "spec_gap":
            run.mark("spec.md", "rejected")
            run.decide(f"spec_gap accepted: {d.get('reason', d.get('gap', 'no detail given'))}. The spec goes back to the planner.")
            say(who, "applied spec_gap: spec.md goes back to the planner")
        elif name == "question_raised" and lane:
            run.set_lane(lane, "blocked")
            say(who, f"{lane} is blocked on a question: {d.get('question', d.get('text', ''))}")
        elif name == "escalate" and lane:
            run.set_lane(lane, "failed")
            say(who, f"{lane} escalated: {d.get('reason', '')}")
        else:
            run.event("orchestrator", "proposal_unclear", {"id": pid, "event": name})
        run.event("orchestrator", "proposal_applied", {"id": pid, "event": name})


def copy_review_decision(run, who):
    p = run.art / "review-report.md"
    if not p.exists():
        return
    m = re.search(r"^human_required:\s*(true|false)\b", p.read_text(encoding="utf-8"), re.M | re.I)
    if not m:
        return
    value = m.group(1).lower() == "true"
    if (run.load().get("review") or {}).get("human_required") != value:
        run.update(lambda man: man.setdefault("review", {}).update(human_required=value))
        run.event("orchestrator", "risk_decision_applied", {"human_required": value})
        say(who, f"copied the reviewer's risk decision: review.human_required = {str(value).lower()}")


def record_outputs(run, cfg, kind, lane, repo, started, who):
    """Mark every artifact this session wrote as ready, with its input hashes. Returns (marked, expected)."""
    view = S.build_view(run.dir, run.load(), cfg, kind, list(cfg["kinds"][kind]["lanes"]), [])
    pk, _, _, _ = S.lane_contract(cfg, kind, lane, set(view))
    inputs = cfg.get("artifact_inputs") or {}
    marked, expected = [], []
    for key in pk:
        expected.append(key)
        path = run.art / "changes" / repo if (key == "changes" and repo) else run.art / key
        if not path.exists() or W.latest_mtime(path) < started - 1:
            continue
        if key == "changes":
            run.mark("changes", "ready", inputs=inputs.get("changes", []), repo=repo, version=run.attempts_done(lane, repo) + 1)
        else:
            run.mark(key, "ready", inputs=inputs.get(key, []))
        marked.append(key)
    return marked, expected


def repo_checkout(run, repo):
    """Find a registered checkout, or the matching seeded demo repo."""
    local = load_cfg("repos.local.yaml").get("repos") or {}
    candidates = []
    if local.get(repo, {}).get("local_path"):
        candidates.append(Path(os.path.expandvars(os.path.expanduser(local[repo]["local_path"]))))
    if os.environ.get("WF_REPOS_ROOT"):
        candidates.append(Path(os.environ["WF_REPOS_ROOT"]) / repo)
    candidates.append(ROOT / "demo" / "seed" / repo)
    return next((path.resolve() for path in candidates if path.is_dir()), None)


def repo_worktree_hash(path):
    """Return a content hash for uncommitted tracked and untracked repository changes."""
    root = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
    )
    if root.returncode:
        detail = root.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"cannot locate Git root for {path}: {detail}")
    git_root = Path(os.fsdecode(root.stdout).strip())
    pathspec = path.relative_to(git_root).as_posix() or "."
    status = subprocess.run(
        ["git", "-C", str(git_root), "status", "--porcelain=v1", "--untracked-files=all", "--", pathspec],
        capture_output=True,
    )
    if status.returncode:
        detail = status.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"cannot inspect repo changes in {path}: {detail}")
    if not status.stdout:
        return None

    diff = subprocess.run(
        ["git", "-C", str(git_root), "diff", "--binary", "HEAD", "--", pathspec],
        capture_output=True,
    )
    if diff.returncode:
        detail = diff.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"cannot hash repo changes in {path}: {detail}")
    untracked = subprocess.run(
        ["git", "-C", str(git_root), "ls-files", "--others", "--exclude-standard", "-z", "--", pathspec],
        capture_output=True,
        check=True,
    ).stdout
    digest = hashlib.sha256(status.stdout + diff.stdout)
    for relative in untracked.split(b"\0"):
        if relative:
            digest.update(relative)
            digest.update((git_root / os.fsdecode(relative)).read_bytes())
    return digest.hexdigest()[:8]


def recheck_artifacts(run):
    """Apply draft/ready states declared in artifact frontmatter to the manifest."""
    manifest = run.load()
    artifacts = manifest.get("artifacts") or {}
    config = S.load_yaml(ROOT / "config" / "lanes.yaml")
    inputs = config.get("artifact_inputs") or {}
    synced = []
    observed = {}

    tasks_path = S.artifact_path(run.dir, "tasks.md")
    task_repos = []
    if tasks_path is not None and str(manifest.get("kind") or "story") != "spike":
        rows = tasks_path.read_text(encoding="utf-8").splitlines()
        repo_column = None
        for line in rows:
            if not line.strip().startswith("|"):
                continue
            cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
            if repo_column is None:
                if "repo" in [cell.lower() for cell in cells]:
                    repo_column = [cell.lower() for cell in cells].index("repo")
                continue
            if all(not cell or set(cell) <= {"-", ":"} for cell in cells):
                continue
            if repo_column < len(cells):
                for repo in cells[repo_column].split(","):
                    repo = repo.strip().strip("`")
                    if repo and repo not in ("-", "none") and repo not in task_repos:
                        task_repos.append(repo)

        _, release_sequence, _ = tasks_release(run)
        for repo in release_sequence:
            if repo not in task_repos:
                task_repos.append(repo)

    for name, entry in artifacts.items():
        if not isinstance(entry, dict):
            continue
        if name == "changes":
            for repo, repo_entry in (entry.get("per_repo") or {}).items():
                if not isinstance(repo_entry, dict) or repo_entry.get("status", "missing") in (
                    "missing", "approved", "rejected",
                ):
                    continue
                path = S.artifact_path(run.dir, f"changes/{repo}")
                if path is None or not path.is_dir():
                    checkout = repo_checkout(run, repo)
                    if checkout is not None and repo_worktree_hash(checkout):
                        continue
                    run.mark("changes", "missing", inputs=(), repo=repo)
                    run.event("orchestrator", "artifact_status_synced", {
                        "artifact": f"changes/{repo}", "status": "missing", "source": "file_missing",
                    })
                    synced.append((f"changes/{repo}", "missing"))
            continue
        current = entry.get("status", "missing")
        path = S.artifact_path(run.dir, name)
        if path is None or not path.is_file():
            if current != "missing":
                run.mark(name, "missing", inputs=())
                run.event("orchestrator", "artifact_status_synced", {
                    "artifact": name, "status": "missing", "source": "file_missing",
                })
                synced.append((name, "missing"))
            continue
        if name == "ticket-context.md":
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0].strip() != "---":
            if current == "missing" and path.stat().st_size > 0:
                run.mark(name, "draft", inputs=inputs.get(name, []))
                run.event("orchestrator", "artifact_status_synced", {
                    "artifact": name, "status": "draft", "source": "file_present",
                })
                synced.append((name, "draft"))
            continue
        try:
            end = lines.index("---", 1)
        except ValueError as exc:
            raise ValueError(f"{name}: frontmatter has no closing --- delimiter") from exc
        metadata = yaml.safe_load("\n".join(lines[1:end])) or {}
        if not isinstance(metadata, dict):
            raise ValueError(f"{name}: frontmatter must contain a YAML mapping")
        status = metadata.get("status")
        if status not in ("draft", "ready"):
            if current == "missing" and path.stat().st_size > 0:
                run.mark(name, "draft", inputs=inputs.get(name, []))
                run.event("orchestrator", "artifact_status_synced", {
                    "artifact": name, "status": "draft", "source": "file_present",
                })
                synced.append((name, "draft"))
            continue
        observed[name] = status
        if status == "ready" and PLACEHOLDER.search("\n".join(lines[end + 1:])):
            raise ValueError(f"{name} still has template placeholders; it cannot be marked ready")

        if current in ("missing", "draft", "ready"):
            recorded_hash = entry.get("hash")
            current_hash = W.short_hash(path)
            if status != current or current_hash != recorded_hash:
                run.mark(name, status, inputs=inputs.get(name, []))
                run.event("orchestrator", "artifact_status_synced", {
                    "artifact": name, "status": status, "source": "frontmatter",
                })
                synced.append((name, status))

    existing_repos = list((((run.load().get("artifacts") or {}).get("changes") or {}).get("per_repo") or {}))
    for repo in dict.fromkeys(task_repos + existing_repos):
        path = S.artifact_path(run.dir, f"changes/{repo}")
        expected = ("diff.patch", "notes.md", "local-verify.md")
        present = [name for name in expected if path is not None and (path / name).is_file()]
        checkout = repo_checkout(run, repo)
        source_hash = repo_worktree_hash(checkout) if checkout is not None else None
        status = "ready" if len(present) == len(expected) else (
            "draft" if present or source_hash else "missing"
        )
        repo_entry = ((run.load().get("artifacts") or {}).get("changes") or {}).get("per_repo", {}).get(repo) or {}
        current = repo_entry.get("status", "missing")
        current_hash = W.short_hash(path) if path is not None and path.is_dir() else None
        if current not in ("approved", "rejected") and (
            not repo_entry or status != current or current_hash != repo_entry.get("hash")
            or source_hash != repo_entry.get("source_hash")
        ):
            run.mark("changes", status, inputs=inputs.get("changes", []), repo=repo)
            run.update(lambda manifest, repo=repo, source_hash=source_hash:
                       manifest["artifacts"]["changes"]["per_repo"][repo].update(
                           {"source_hash": source_hash} if source_hash else {}
                       ))
            if not source_hash:
                run.update(lambda manifest, repo=repo:
                           manifest["artifacts"]["changes"]["per_repo"][repo].pop("source_hash", None))
            run.event("orchestrator", "artifact_status_synced", {
                "artifact": f"changes/{repo}", "status": status,
                "source": "repo_checkout" if source_hash and not present else "repo_change_outputs",
                "source_hash": source_hash,
            })
            synced.append((f"changes/{repo}", status))

    review_entry = ((run.load().get("artifacts") or {}).get("review-report.md") or {})
    if isinstance(review_entry, dict) and review_entry.get("status") in ("ready", "approved"):
        copy_review_decision(run, "orchestrator")

    lanes = (manifest.get("lanes") or {})
    for lane, definition in (config.get("lanes") or {}).items():
        lane_entry = lanes.get(lane) or {}
        if lane_entry.get("status") != "failed":
            continue
        outputs = [S.artifact_key(S.split_token(token)[0], set(artifacts))
                   for token in definition.get("produces") or []]
        outputs = [name for name in outputs if name]
        recovered = [name for name in outputs if observed.get(name) in ("draft", "ready")]
        if recovered:
            run.set_lane(lane, "waiting")
            run.update(lambda current_manifest, lane=lane: current_manifest.setdefault("lanes", {})
                       .setdefault(lane, {}).update(retry_after_recheck=True))
            run.event("orchestrator", "lane_reopened", {
                "lane": lane, "artifacts": recovered, "reason": "artifact progress found during recheck",
            })
            say("orchestrator", f"reopened {lane} after finding updated artifacts")

    if synced:
        for name, status in synced:
            say("orchestrator", f"synced {name}: {status}")
    else:
        say("orchestrator", "no artifact status changes found in frontmatter")


# ---------------------------------------------------------------- one session

def run_unit(run, cfg, kind, runner, budgets, lane, repo, who, verbose):
    unit = f"{lane}-{repo}" if repo else lane
    attempt = run.attempts_done(lane, repo) + 1
    max_attempts = int((budgets.get("per_lane") or {}).get("max_attempts", 3))
    if run.attempts_in_cycle(lane, repo) + 1 > max_attempts:
        run.set_lane(lane, "failed")
        run.event("orchestrator", "lane_failed", {"lane": lane, "repo": repo, "reason": f"more than {max_attempts} attempts"})
        say(who, f"{unit}: gave up after {max_attempts} attempts; escalating")
        run.release(unit)
        return
    cmd = agent_command(runner, run, lane, repo, attempt)
    launch = [PY, str(HERE / "wf_launch.py"), "--ticket", run.ticket, "--run", str(run.run), "--lane", lane,
              "--attempt", str(attempt)] + (["--repo", repo] if repo else []) + ["--"] + cmd
    logdir = run.dir / "logs"
    logdir.mkdir(exist_ok=True)
    log = logdir / f"{unit}-a{attempt}.log"
    run.set_lane(lane, "working", repo or None)
    say(who, f"{unit}: starting attempt {attempt}")
    started = time.time()
    with open(log, "w", encoding="utf-8", errors="replace") as lf:
        p = subprocess.Popen(launch, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace")
        for line in p.stdout:
            lf.write(line)
            if verbose:
                say(who, line.rstrip())
        rc = p.wait()
    secs = round(time.time() - started, 1)

    chk = [PY, str(HERE / "otel_check.py"), "--ticket", run.ticket, "--run", str(run.run), "--lane", lane, "--attempt", str(attempt)]
    if repo:
        chk += ["--repo", repo]
    r = subprocess.run(chk, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = r.stdout.splitlines()
    otel = (out[0].split(":", 1)[1].strip() if out and out[0].startswith("otel_status:") else "not checked")
    tok = next((l.strip() for l in out if "tokens in/out" in l), "")

    marked, expected = [], []
    if rc == 0:
        marked, expected = record_outputs(run, cfg, kind, lane, repo, started, who)
    apply_proposals(run, who)
    if lane == "reviewer":
        copy_review_decision(run, who)
    proposed = any(e["event"] in S.PROPOSALS and e["actor"] not in ("orchestrator", "launcher")
                   and e["event"] != "question_raised" for e in run.events()[-50:]
                   if e["time"] >= time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started - 1)))
    outcome = "crashed" if rc != 0 else ("ok" if (expected and len(marked) == len(expected)) or proposed else "incomplete")
    blocked = (run.load().get("lanes") or {}).get(lane)
    blocked = blocked.get("status") if isinstance(blocked, dict) and not repo else None
    if blocked == "blocked" and outcome == "incomplete":
        outcome = "blocked"
    if blocked not in ("blocked", "failed"):
        run.set_lane(lane, "waiting", repo or None)
    run.event(f"worker:{lane}", "lane_finished", {"lane": lane, "repo": repo, "attempt": attempt, "returncode": rc,
                                                  "outcome": outcome, "seconds": secs, "artifacts": marked,
                                                  "otel": otel, "log": log.relative_to(ROOT).as_posix()})
    run.release(unit)
    say(who, f"{unit}: attempt {attempt} {outcome} in {secs}s; marked {', '.join(marked) or 'nothing'}; telemetry {otel} {tok}".rstrip())
    if outcome != "ok":
        say(who, f"{unit}: see {log.relative_to(ROOT).as_posix()}")


# ---------------------------------------------------------------- workers

def lane_worker(args):
    run = W.Run(args.ticket, args.run)
    cfg = S.load_yaml(ROOT / "config" / "lanes.yaml")
    runner, budgets = load_cfg("runner.yaml"), load_cfg("budgets.yaml")
    poll = float(runner.get("poll_seconds", 5))
    idle_limit = float(runner.get("idle_exit_minutes", 120)) * 60
    lease_min = float((budgets.get("per_lane") or {}).get("lease_minutes", 30))
    lane, who = args.lane, args.lane
    threads, last_busy, said = {}, time.time(), None
    while True:
        res = S.analyze(args.ticket, args.run)
        kind = res["kind"]
        rec = res["lanes"].get(lane)
        if rec is None or rec["state"] == "disabled":
            say(who, "not part of this run (disabled or not in this kind); exiting")
            return
        for u in [u for u, t in threads.items() if not t.is_alive()]:
            del threads[u]
        for repo in launchable(res, lane, run, 3):
            if repo in threads:
                continue
            unit = f"{lane}-{repo}" if repo else lane
            if not run.acquire(unit, lease_min):
                continue
            t = threading.Thread(target=run_unit, args=(run, cfg, kind, runner, budgets, lane, repo, who, args.verbose), daemon=True)
            threads[repo] = t
            t.start()
            last_busy = time.time()
        if threads:
            last_busy = time.time()
        finished = res["status"] in ("Done", "Escalated")
        if finished and not threads and not launchable(res, lane, run, 3) and not (lane == "inspect-adapt" and rec["state"] not in ("done", "blocked", "failed")):
            say(who, f"run is {res['status'].lower()}; exiting")
            return
        if not threads and time.time() - last_busy > idle_limit:
            say(who, f"idle for {idle_limit / 60:.0f} minutes; exiting")
            return
        wait = f"{rec['state']}: {rec['note']}" if not threads else None
        if wait and wait != said:
            say(who, f"waiting ({wait[:110]})")
            said = wait
        time.sleep(poll)


def ci_worker(args):
    """CI/CD stand-in. Not a lane: it follows the Release order once the review has passed."""
    run = W.Run(args.ticket, args.run)
    runner = load_cfg("runner.yaml")
    cmd_t = json.loads(os.environ["WF_DEPLOY_CMD"]) if os.environ.get("WF_DEPLOY_CMD") else (runner.get("deploy") or {}).get("command")
    poll = float(runner.get("poll_seconds", 5))
    who, said = "ci", None
    while True:
        res = S.analyze(args.ticket, args.run)
        if res["status"] in ("Done", "Escalated"):
            say(who, f"run is {res['status'].lower()}; exiting")
            return
        if res["status"] == "Deploying" and cmd_t:
            m = run.load()
            rel = m.get("release") or {}
            repos = list(((m.get("lanes") or {}).get("dev") or {}).get("repos") or {})
            order = [str(x) for x in rel.get("sequence") or []] if rel.get("deploy_order") == "ordered" else repos
            state = rel.get("repos") or {}
            todo = [r for r in order if state.get(r) != "deployed"]
            if todo and state.get(todo[0]) != "failed":
                repo = todo[0]
                run.event("ci_cd", "deploy_started", {"repo": repo})
                cmd = [str(x).format(repo=repo, ticket=run.ticket, run=run.run, root=str(ROOT)) for x in (cmd_t if isinstance(cmd_t, list) else cmd_t.split())]
                r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
                ok = r.returncode == 0
                run.update(lambda man: man.setdefault("release", {}).setdefault("repos", {}).update({repo: "deployed" if ok else "failed"}))
                run.event("ci_cd", "deployed" if ok else "deploy_failed", {"repo": repo})
                say(who, f"{repo}: {'deployed' if ok else 'deploy FAILED'}" + ("" if ok else "\n" + (r.stdout + r.stderr)[-600:]))
                continue
        msg = "waiting for the review" if res["status"] != "Deploying" else ("waiting for a person: wf_run.py deployed --repo <name>" if not cmd_t else "")
        if msg and msg != said:
            say(who, msg)
            said = msg
        time.sleep(poll)


def auto_human(args):
    """Plays the human at every gate. Testing only."""
    run = W.Run(args.ticket, args.run)
    poll = float(load_cfg("runner.yaml").get("poll_seconds", 5))
    who, by = "auto-human", "auto-approve (test)"
    while True:
        res = S.analyze(args.ticket, args.run)
        if res["status"] in ("Done", "Escalated"):
            return
        for gate, g in (res.get("gates") or {}).items():
            if g["state"] == "awaiting_approval":
                say(who, f"approving the {gate} gate")
                do_approve(run, gate, by)
        if res["status"] == "Awaiting acceptance":
            say(who, "accepting the ticket")
            do_accept(run, by)
        time.sleep(poll)


# ---------------------------------------------------------------- human commands

def tasks_release(run):
    text = (run.art / "tasks.md").read_text(encoding="utf-8")
    order = re.search(r"deploy_order:\s*(\w+)", text)
    seq = re.search(r"sequence:\s*([\w\-, ]+)", text)
    sequence = [s.strip() for s in seq.group(1).split(",") if s.strip()] if seq else []
    reg = list((S.load_yaml(ROOT / "config" / "repos.yaml").get("repos") or {})) if (ROOT / "config" / "repos.yaml").exists() else []
    repos = [r for r in reg if re.search(r"(?<![\w-])" + re.escape(r) + r"(?![\w-])", text)]
    return (order.group(1) if order else "independent"), sequence, repos


def gate_keys(run, gate):
    cfg = S.load_yaml(ROOT / "config" / "lanes.yaml")
    kind = str(run.load().get("kind") or "story")
    if kind == "spike" and gate == "planner":
        return ["findings.md"], kind
    lane = cfg["lanes"][gate]
    return list((lane.get("gate") or {}).get("artifacts") or []), kind


def do_approve(run, gate, by):
    keys, kind = gate_keys(run, gate)
    res = S.analyze(run.ticket, run.run)
    g = (res.get("gates") or {}).get(gate)
    if not g or g["state"] != "awaiting_approval":
        sys.exit(f"error: the {gate} gate is not waiting for approval (it is {g['state'] if g else 'not applicable'}). "
                 f"Ticket status: {res['status']} - {res['detail']}")
    for k in keys:
        run.mark(k, "approved", approved_by=by)
    run.event(by, "gate_approved", {"gate": gate, "artifacts": keys})
    if gate == "planner" and kind != "spike":
        order, seq, repos = tasks_release(run)
        def fn(m):
            m.setdefault("release", {}).update(deploy_order=order, sequence=seq)
            m.setdefault("lanes", {}).setdefault("dev", {})["repos"] = {r: "waiting" for r in repos}
        run.update(fn)
        run.event("orchestrator", "manifest_updated", {"repos": repos, "deploy_order": order, "sequence": seq})
        run.decide(f"{by} approved {', '.join(keys)} together. Repos {repos} and Release ({order}: {', '.join(seq) or '-'}) copied from tasks.md.")
        print(f"approved. Dev repos from tasks.md: {', '.join(repos) or '(none found: check tasks.md and config/repos.yaml)'}; deploy_order: {order}")
    else:
        if kind == "spike":      # approving the findings is the spike's acceptance; record it where agents can see it
            run.update(lambda m: m.setdefault("acceptance", {}).update(accepted=True, signed_off_by=by))
            run.event(by, "ticket_accepted", {"via": "findings approved"})
        run.decide(f"{by} approved {', '.join(keys)}.")
        print(f"approved {', '.join(keys)}")


def do_accept(run, by):
    spike = str(run.load().get("kind")) == "spike"
    run.update(lambda m: m.setdefault("acceptance", {}).update(**({"accepted": True} if spike else {"ticket_accepted": True}), signed_off_by=by))
    run.event(by, "ticket_accepted", {})
    run.decide(f"{by} accepted the ticket.")
    print("ticket accepted")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["start", "worker", "command", "ready", "recheck", "approve", "reject", "answer", "enable", "accept", "deployed", "status"])
    ap.add_argument("--ticket", required=True)
    ap.add_argument("--run", type=int, default=None)
    ap.add_argument("--lane", default="")
    ap.add_argument("--gate", default="planner", choices=["planner", "reviewer"])
    ap.add_argument("--artifact", default="ticket-context.md")
    ap.add_argument("--repo", default="")
    ap.add_argument("--by", default=os.environ.get("USERNAME") or os.environ.get("USER") or "human")
    ap.add_argument("--reason", default="")
    ap.add_argument("--text", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--auto-approve", action="store_true", help="play the human at every gate (testing only)")
    ap.add_argument("--verbose", action="store_true", help="show the agent's output as it runs")
    args = ap.parse_args()
    if args.run is None:
        tdir = ROOT / "tickets" / args.ticket
        args.run = S.latest_run(tdir) if tdir.is_dir() else None
        if args.run is None:
            sys.exit(f"error: no run found for {args.ticket}")

    if args.command == "status":
        sys.exit(subprocess.call([PY, str(HERE / "wf_status.py"), args.ticket, "--run", str(args.run)]))
    if args.command == "worker":
        if not args.lane:
            sys.exit("error: --lane is required")
        try:
            return ci_worker(args) if args.lane == "ci" else auto_human(args) if args.lane == "auto-human" else lane_worker(args)
        except KeyboardInterrupt:
            return
    run = W.Run(args.ticket, args.run)
    if args.command == "command":
        cmd = agent_command(load_cfg("runner.yaml"), run, args.lane or "planner", args.repo, run.attempts_done(args.lane or "planner", args.repo) + 1)
        print(json.dumps(cmd, indent=2, ensure_ascii=False))
        return
    if args.command == "start":
        return start(args, run)
    if args.command == "recheck":
        return recheck_artifacts(run)
    if args.command == "ready":
        p = run.art / args.artifact
        if not p.exists():
            sys.exit(f"error: {p} not found")
        if PLACEHOLDER.search(p.read_text(encoding="utf-8")) and not args.force:
            sys.exit(f"error: {args.artifact} still has template placeholders such as <paste ...>. Fill them in, or use --force.")
        run.mark(args.artifact, "ready")
        run.event(args.by, "artifact_ready", {"artifact": args.artifact})
        print(f"{args.artifact} marked ready")
    elif args.command == "approve":
        do_approve(run, args.gate, args.by)
    elif args.command == "reject":
        keys, _ = gate_keys(run, args.gate)
        for k in keys:
            run.mark(k, "rejected")
        run.event(args.by, "gate_rejected", {"gate": args.gate, "artifacts": keys, "reason": args.reason})
        run.decide(f"{args.by} rejected {', '.join(keys)}: {args.reason or 'no reason given'}")
        print(f"rejected {', '.join(keys)}; the lane will start again")
    elif args.command == "answer":
        if not args.lane:
            sys.exit("error: --lane is required")
        run.set_lane(args.lane, "waiting")
        run.event(args.by, "question_answered", {"lane": args.lane, "text": args.text})
        run.decide(f"{args.by} answered the {args.lane} question: {args.text or '(see events.log)'}")
        print(f"{args.lane} unblocked")
    elif args.command == "enable":
        if not args.lane:
            sys.exit("error: --lane is required")
        run.update(lambda m: m.setdefault("lanes", {}).setdefault(args.lane, {}).update(enabled=True))
        run.event(args.by, "lane_enabled", {"lane": args.lane})
        print(f"{args.lane} enabled; start the workers again: python scripts/wf_run.py start --ticket {args.ticket}")
    elif args.command == "accept":
        do_accept(run, args.by)
    elif args.command == "deployed":
        if not args.repo:
            sys.exit("error: --repo is required")
        rel = run.load().get("release") or {}
        seq = [str(x) for x in rel.get("sequence") or []]
        if rel.get("deploy_order") == "ordered" and args.repo in seq:
            for earlier in seq[:seq.index(args.repo)]:
                if (rel.get("repos") or {}).get(earlier) != "deployed":
                    sys.exit(f"error: release order says {earlier} must be deployed before {args.repo}")
        run.update(lambda m: m.setdefault("release", {}).setdefault("repos", {}).update({args.repo: "deployed"}))
        run.event("ci_cd", "deployed", {"repo": args.repo, "recorded_by": args.by})
        print(f"{args.repo} recorded as deployed")


def start(args, run):
    res = S.analyze(args.ticket, args.run)
    kind = res["kind"]
    lanes = [l for l, r in res["lanes"].items() if r["state"] != "disabled"]
    if kind != "spike":
        lanes.append("ci")
    if args.auto_approve:
        lanes.append("auto-human")
    if "--agent" in (load_cfg("runner.yaml").get("agent") or {}).get("command", []) and not os.environ.get("WF_AGENT_CMD"):
        missing = [l for l in lanes if l != "ci" and l != "auto-human" and not (ROOT / ".github" / "agents" / f"wf-{l}.agent.md").exists()]
        if missing:
            print("warning: no agent file for: " + ", ".join(missing) + " (expected .github/agents/wf-<lane>.agent.md)\n")
    print(f"Starting run {args.ticket}/run-{args.run} ({kind}). Workers: {', '.join(lanes)}")
    print("Each worker acts on its own when its lane is ready. Ctrl+C stops them all.\n")
    procs = []
    for lane in lanes:
        cmd = [PY, str(HERE / "wf_run.py"), "worker", "--ticket", args.ticket, "--run", str(args.run), "--lane", lane]
        if args.verbose:
            cmd.append("--verbose")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", env=env)
        procs.append(p)
        threading.Thread(target=lambda p=p: [print(l.rstrip(), flush=True) for l in p.stdout], daemon=True).start()
    asked = None
    try:
        while any(p.poll() is None for p in procs):
            cur = S.analyze(args.ticket, args.run)
            humans = [a["text"] for a in cur["next"] if a["who"].lower().startswith("human")]
            key = (cur["status"], tuple(humans))
            if humans and key != asked and not args.auto_approve:
                print(f"\n>>> Waiting for a person ({cur['status']}):", flush=True)
                for h in humans:
                    print(f"    {h}", flush=True)
                asked = key
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping workers...", flush=True)
        for p in procs:
            if p.poll() is None:
                p.terminate()
        return
    final = S.analyze(args.ticket, args.run)
    print(f"\nRun finished: {final['status']} - {final['detail']}")
    print(f"Details: python scripts/wf_status.py {args.ticket}")
    sys.exit(0 if final["status"] == "Done" else 1)


if __name__ == "__main__":
    main()
