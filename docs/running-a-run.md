# Running a run: one trigger, every lane works on its own

There is no central controller and no fixed pipeline. A run is a set of **lane workers**. Each one is a small loop that:

1. derives the state from the files (`wf_status.py` logic),
2. when its lane is ready and nobody holds it, takes a **lease**,
3. launches one agent session through `wf_launch.py` (so telemetry is tagged),
4. runs `otel_check.py`, marks the artifacts that session wrote as `ready` with their input hashes,
5. applies the events agents proposed (`verify_failed`, `spec_gap`, `question_raised`, ...), once each,
6. looks again.

Workers share nothing but the run folder. You can run them all with one command or one per terminal.

## Start a run

```
python scripts/new_ticket.py --ticket PROJ-1 --kind story      # then fill artifacts/ticket-context.md
python scripts/wf_run.py ready --ticket PROJ-1                 # intake done: the Planner may start
python scripts/wf_run.py start --ticket PROJ-1                 # the one trigger
```

Use `python scripts/wf_run.py recheck --ticket T` to reconcile artifact frontmatter and repo change outputs with the run manifest. Repo changes are ready when `diff.patch`, `notes.md`, and `local-verify.md` are present; uncommitted changes in a registered checkout appear as draft. For a ready review report, recheck copies its explicit `human_required` decision into the manifest. Ticket context remains controlled by the explicit `ready` command. If a failed lane has draft/ready outputs, recheck reopens it and resets its retry budget.

`start` launches one worker per lane (plus `ci`, the CI/CD stand-in, for stories and chores) and prints what they do. It stops when the ticket is Done or Escalated. Add `--verbose` to see the agents' output; it is always saved to `logs/` in the run folder.

## What humans do

The workers stop at a human gate and `start` prints the exact command. Nothing is edited by hand any more.

| When | Command |
|---|---|
| Plan (or spike findings) is ready | `wf_run.py approve --ticket T --gate planner` or `reject --gate planner --reason "..."` |
| Review is risk-flagged | `wf_run.py approve --ticket T --gate reviewer` |
| An agent asked a question (`question_raised`) | `wf_run.py answer --ticket T --lane planner --text "..."` |
| Deployed outside the workers (no `deploy.command`) | `wf_run.py deployed --ticket T --repo R` |
| Acceptance (policy still TBD) | `wf_run.py accept --ticket T` |

## Rules the workers follow

- **Lease:** one session per lane instance (per repo for Dev), created atomically. A crashed session's lease expires (`config/budgets.yaml`, `lease_minutes`).
- **Attempts:** each launch gets the next attempt number, counted from `lane_finished` events. After `max_attempts` the lane is marked failed and the ticket is Escalated.
- **Outcome:** `ok` (artifacts written, or a proposal applied), `incomplete` (nothing usable), `crashed` (non-zero exit), `blocked` (a question). Incomplete and crashed sessions are retried.
- **Rework:** `verify_failed` rejects `changes/<repo>` only; that Dev instance starts again and the verify report goes stale, which sends the Reviewer back.
- **Release order:** the `ci` worker deploys in the order from `tasks.md` and stops on the first failure.
- **Manifest writes** are atomic and locked; only workers and the commands above write it. Agents never do.

## Agents: one per lane

Each lane is a Copilot **custom agent**, not a prompt. The role, the rules and the allowed tools live in `.github/agents/wf-<lane>.agent.md`; the task message only says which work item to do it for.

- **The agent file is the lane's instructions.** Edit `.github/agents/wf-<lane>.agent.md` directly: the frontmatter sets the description and the allowed `tools` (and optionally `model`), the body is what the lane does. There is no build step. Rules shared by every lane stay in `skills/_common.md`, which each agent reads first; artifact templates stay in `skills/<lane>/templates/`.
- **Launch:** `copilot --agent wf-<lane> -p "<task>" --allow-all-tools --no-ask-user --add-dir <root> ...` (see `config/runner.yaml`; print the exact command with `wf_run.py command --ticket T --lane L [--repo R]`). `--add-dir <root>` is what lets a Dev session running inside a repo still find the agents.
- **Permissions are enforced by the agent's `tools`:** for example the Planner has no shell, so a spike cannot run commands. Per-lane extras (`--deny-tool`, `--max-ai-credits`) go under `agent.by_lane`.
- **Unverified:** the agent file format and the tool aliases (`read`, `search`, `edit`, `execute`) follow GitHub's custom agent format as I know it, and `--agent wf-planner` has not been run yet. First check: `copilot --agent wf-planner -p "say your lane and the tools you have" --allow-all-tools`. If it does not load, the frontmatter of the five agent files is what to adjust.

To use any other command, set `WF_AGENT_CMD` (a JSON list); the demo does this to use scripted stub agents.

## Try it without AI

```
python demo/demo.py auto                    # the whole DEMO-1 story in a fresh workspace, humans simulated
python demo/demo.py auto --gates-by-hand    # same, but you approve at the gates
```

`--auto-approve` on `start` plays the human at every gate. It is for testing the flow only.
