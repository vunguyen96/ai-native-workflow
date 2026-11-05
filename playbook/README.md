# Workflow playbook

The local playbook displays ticket, lane, and artifact readiness and provides controls for the existing workflow commands.

## Start the playbook

Requires Python on `PATH`. From the repository root, run:

```powershell
.\playbook\start.ps1
```

The script starts the local server on port `8765`. Open [http://127.0.0.1:8765](http://127.0.0.1:8765) in your browser. The server binds to localhost only; press `Ctrl+C` in the terminal to stop it.

To use a different port, pass it to the script and open the matching URL:

```powershell
.\playbook\start.ps1 -Port 8766
```

## Human decisions

- **Recheck status** reconciles artifact frontmatter, repo change outputs, and the reviewer risk decision with the run manifest.
- **Approve review** appears only while the reviewer gate is awaiting approval, and invokes `wf_run.py approve --ticket T --gate reviewer`.
- **Confirm deployment complete** appears during deployment. For ordered releases, the playbook offers only the next repository in the release sequence. Confirm only after that repository has actually been deployed; the action records it with `wf_run.py deployed --ticket T --repo R`.
- **Accept ticket** appears only after all required deployments are recorded. It invokes `wf_run.py accept --ticket T`.
- **Launch Inspect & Adapt** appears when that lane is ready. It invokes `wf_launch.py` with the selected ticket, run, next attempt number, and the Copilot command configured in `config/runner.yaml` (or `WF_AGENT_CMD`).

The playbook validates that an action is currently allowed before launching it. Workflow state and gate rules remain owned by the scripts under `scripts/`; the playbook is their UI, not a separate workflow implementation.
