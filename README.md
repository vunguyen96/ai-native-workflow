# LEGO AI workflow

PI planning gives a PRD per topic. Each PRD is split into tickets, and each ticket triggers one run of this flow. Agents work independently by role (lane) and share only artifacts. Status is tracked by artifact and lane statuses, never by a fixed pipeline.

Design overview for presenting the workflow: [docs/workflow-design.md](docs/workflow-design.md).

## Lanes

| Lane | Needs | Produces |
|---|---|---|
| Planner | `ticket-context.md` | `spec.md`, `design.md`, `tasks.md` (human gate) |
| QE | approved spec and tasks | `test-plan.md`, `tests/<repo>/` |
| Dev (per repo) | approved spec, design, tasks | `changes/<repo>/` |
| Reviewer | all changes, test plan | `verify-report.md`, `review-report.md` (human gate when risk-flagged) |
| Inspect & Adapt | whole trail and telemetry | `inspection-report.md`, ledger entries, overlay candidates |

Merge and deploy are plain CI/CD after review passes, not a lane. QE and Dev start together once the spec is approved. Reviewer waits for both.

## Learning loop

When a ticket is accepted, Inspect & Adapt runs automatically, using the OTEL metrics of the lane sessions. It writes candidates to `overlay/` (a removable layer on top of the lane agents in `.github/agents/`) and logs each one in `ledger/`. At every sprint retro the team reviews the whole ledger and decides what to promote into `skills/`. Limits for what Adapt may do are in `config/overlay-rules.yaml`.

## Multiple repos

Many tickets span several repos. The Planner lists the repos in `tasks.md` (names must match `config/repos.yaml`) and one Dev instance runs per repo, launched with `--repo`. A failure in one repo re-runs only that repo's Dev lane.

- **Registry:** `config/repos.yaml` holds the shared list (name, remote, default branch). Local paths differ per developer, so they are not stored there: copy `config/repos.local.example.yaml` to `config/repos.local.yaml` (git-ignored), or set `WF_REPOS_ROOT` so that repo X is found at `$WF_REPOS_ROOT/X`.
- **Launcher:** with `--repo`, the session starts in that repo's folder and gets `WF_REPO` and `WF_REPO_PATH`. Every session also gets `WF_REPO_PATHS`, a JSON map of all registered repos, so the Planner and Reviewer can see them.
- **Release order:** `tasks.md` ends with a Release section, `deploy_order: independent` or `ordered` (with the sequence). Both cases are expected. An ordered deploy across repos triggers a human review; independent multi-repo changes do not.
- **Placeholders, to be improved by the team:** cross-repo tests (QE lists integration scenarios; automation optional) and release order handling (CI/CD follows the declared order and stops and escalates on the first failure; rollback is manual). See `placeholders` in `config/lanes.yaml`.

## Work kinds

The unit of work is a work item with a `kind`. `story` is the default.

| Kind | Source of "what to do" | Lanes | Notes |
|---|---|---|---|
| `story` | Jira criteria and PRD requirement ids | all | traceability: Jira criteria to ACs |
| `chore` | Intent and done criteria (C-n) in `ticket-context.md` | all | no PRD; counts as capacity only; tagged by `--origin` |
| `spike` | A question, the decision it informs, a time box | Planner only, producing `findings.md` | no code, tests or deploy; a human accepts the findings; Inspect & Adapt off unless enabled |

Metrics are compared only within the same kind. Hotfix is not supported yet.

## Layout

```
config/      lanes.yaml, repos.yaml, risk-rules.yaml, budgets.yaml, overlay-rules.yaml   (humans edit)
skills/      _common.md (rules shared by every lane) and templates/ per lane  (changed by retro promotion only)
.github/agents/  wf-<lane>.agent.md: each lane's instructions and allowed tools  (changed by retro promotion only)
overlay/     active candidates per lane                                        (written by Adapt)
ledger/      one file per entry, template in _template.yaml
tickets/     tickets/<TICKET>/run-<N>/ with manifest.yaml, events.log, decisions.md, artifacts/, metrics/
otel-dump/   raw telemetry per session (git-ignored, may contain sensitive content)
scripts/     new_ticket.py, wf_launch.py, otel_check.py, wf_status.py (Python 3; wf_status.py needs PyYAML, see requirements.txt)
demo/        runnable demo with fake repos and scripted agents (workspace is git-ignored), see demo/README.md
```

## Quick start

One-time setup (Python 3.8 or newer): `python -m pip install -r requirements.txt`. Only `wf_status.py` needs it.

```
python scripts/new_ticket.py --ticket PROJ-123 --prd PRD-12#R4                  # story
python scripts/new_ticket.py --ticket CHORE-7 --kind chore --origin tech-debt    # chore
python scripts/new_ticket.py --ticket SPIKE-2 --kind spike --origin experiment   # spike
python scripts/wf_launch.py --ticket PROJ-123 --lane planner -- copilot <your usual arguments>
python scripts/otel_check.py --ticket PROJ-123 --lane planner
python scripts/wf_status.py PROJ-123                 # where it stands, what is ready, what to do next
python scripts/wf_status.py --all                    # one line per work item
```

## Try the demo

To see a whole ticket run without real repos or agents: `python demo/demo.py setup`, then `python demo/demo.py next` once per step. It uses two fake repos and scripted stand-in agents, in a separate git-ignored workspace. A talk track is in [demo/WALKTHROUGH.md](demo/WALKTHROUGH.md).

An animated overview with the demo (about 2 minutes, open in a browser) is in [docs/presentation/workflow-animation.html](docs/presentation/workflow-animation.html). It pauses by itself after every scene and every demo step so you can talk; Space continues, PageDown and PageUp jump between sections, and `?breakpoints=0` turns the pauses off.

For a live status board with clickable workflow controls, follow [playbook/README.md](playbook/README.md). It derives ticket, lane, and artifact statuses through `wf_status.py`; controls invoke the existing `new_ticket.py` and `wf_run.py` scripts. The server binds to localhost only.

`wf_launch.py` turns on the Copilot OTel file exporter, tags the session with `workflow.*` attributes (ticket, kind, run, lane, repo, attempt, launch_id, active overlay candidates), writes the dump under `otel-dump/`, and logs `lane_started` and `lane_exited` in `events.log`. `otel_check.py` verifies the attributes and token counts and writes a summary to the ticket's `metrics/` folder. Run it after every lane session; `missing` or `incomplete` stops Inspect & Adapt from drawing conclusions about that lane.

For VS Code sessions, attributes are per window, not per lane. Use `--print-env` to see the variables, or record the `session.id` for the lane in `events.log` and join on it.

## Status

Confirmed: lanes and names, artifact-driven readiness, derived ticket status, overlay mode for Adapt, retro review of the ledger, OTEL as the data source, risk-based human review.

`wf_status.py` is the read-only first slice of the orchestrator. For a work item it prints the ticket status, the state of every lane, the effective status of every artifact, the next actions, the proposed events from `events.log`, and warnings about hand-edit mistakes. It never writes a file. It derives:

- **Staleness.** Record `hash:` on an artifact and `inputs:` (artifact name to hash) on whatever was built from it. If a file no longer matches its recorded hash, or an input changed, the artifact shows as stale, and everything built on it goes stale down the lane contracts. Get a hash with `python scripts/wf_status.py --hash <file>`, and quote it in YAML.
- **Lane state** from artifact statuses, with the human gates (spec approval, risk-flagged review) and the per-repo Dev states. The manifest's lane status counts only for `working`, `blocked` and `failed`.
- **Ticket status:** Planning, Building, Verifying, In review, Deploying, Awaiting acceptance, Done, Needs input, Escalated (spikes: Investigating, Awaiting findings review).
- **Risk decision.** The Reviewer's decision lives in `review.human_required` (true or false). Until it is set, a ready review shows as waiting for that decision.

The write side is `scripts/wf_state.py` (manifest, events, leases) and `scripts/wf_run.py` (lane workers and the human commands). See [docs/running-a-run.md](docs/running-a-run.md). Try it on the scripted agents with `python demo/demo.py auto`: one command, and every lane works on its own. Not built yet: auto-revert of overlays, the `next-tasks` queue, and anything that needs the real acceptance policy.

Open: acceptance policy (suggested default in `manifest.yaml`). Confirmed on one real Copilot CLI dump: the `workflow.*` tags arrive as resource attributes, the session id is `gen_ai.conversation.id`, and per-call `github.copilot.cost` and `github.copilot.nano_aiu` are present. Still to confirm: how `nano_aiu` maps to billing, and VS Code and IntelliJ sessions.
