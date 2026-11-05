#!/usr/bin/env python3
"""Serve a local workflow playbook for viewing and triggering ticket runs."""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import wf_status  # noqa: E402
import wf_state  # noqa: E402
import wf_run  # noqa: E402

PLAYBOOK = ROOT / "playbook" / "index.html"
TICKET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
LANES = ("planner", "qe", "dev", "reviewer", "inspect-adapt")
KINDS = ("story", "chore", "spike")
jobs = {}
jobs_lock = threading.Lock()


def valid_ticket(value):
    if not isinstance(value, str) or not TICKET_RE.fullmatch(value):
        raise ValueError("Ticket IDs may contain letters, numbers, dots, underscores and hyphens.")
    return value


def analyze(ticket, run):
    valid_ticket(ticket)
    if run is not None and (not isinstance(run, int) or isinstance(run, bool) or run < 1):
        raise ValueError("Run must be a positive integer.")
    return wf_status.analyze(ticket, run)


def command_for(action):
    ticket = valid_ticket(action.get("ticket"))
    if action.get("action") == "create":
        kind = action.get("kind", "story")
        if kind not in KINDS:
            raise ValueError("Unsupported work item kind.")
        return [sys.executable, str(ROOT / "scripts" / "new_ticket.py"), "--ticket", ticket, "--kind", kind]

    res = analyze(ticket, action.get("run"))
    base = [sys.executable, str(ROOT / "scripts" / "wf_run.py")]
    verb = action.get("action")
    args = ["--ticket", ticket, "--run", str(res["run"])]
    if verb == "recheck":
        return base + ["recheck"] + args
    if verb == "ready":
        return base + ["ready"] + args
    if verb == "start":
        return base + ["start"] + args
    if verb == "worker":
        lane = action.get("lane")
        if not isinstance(lane, str) or lane not in res["lanes"] or res["lanes"][lane]["state"] == "disabled":
            raise ValueError("That lane is not enabled for this work item.")
        return base + ["worker"] + args + ["--lane", lane]
    if verb == "inspect-adapt":
        lane = "inspect-adapt"
        lane_info = res["lanes"].get(lane)
        if (
            not lane_info
            or (
                lane_info["state"] not in ("ready", "stale")
                and not lane_info.get("resumable")
            )
            or lane_info.get("unmet")
        ):
            raise ValueError("Inspect & Adapt is not ready to launch.")
        run = wf_state.Run(ticket, res["run"])
        attempts = sum(
            1 for event in wf_status.read_events(run.dir / "events.log")
            if event["event"] == "lane_started" and event["detail"].get("lane") == lane
        )
        attempt = attempts + 1
        try:
            copilot_command = wf_run.agent_command(
                wf_run.load_cfg("runner.yaml"), run, lane, None, attempt
            )
        except SystemExit as exc:
            raise ValueError(str(exc)) from exc
        return [
            sys.executable, str(ROOT / "scripts" / "wf_launch.py"),
            "--ticket", ticket, "--run", str(res["run"]), "--lane", lane,
            "--attempt", str(attempt), "--", *copilot_command,
        ]
    if verb == "approve":
        gate = action.get("gate")
        if (not isinstance(gate, str) or gate not in ("planner", "reviewer")
                or (res.get("gates") or {}).get(gate, {}).get("state") != "awaiting_approval"):
            raise ValueError("Unsupported approval gate.")
        return base + ["approve"] + args + ["--gate", gate]
    if verb == "reject":
        gate, reason = action.get("gate"), action.get("reason")
        if (not isinstance(gate, str) or gate not in ("planner", "reviewer")
                or (res.get("gates") or {}).get(gate, {}).get("state") != "awaiting_approval"):
            raise ValueError("That gate is not waiting for a human decision.")
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
            raise ValueError("A rejection reason is required (up to 1,000 characters).")
        return base + ["reject"] + args + ["--gate", gate, "--reason", reason.strip()]
    if verb == "answer":
        lane, raw_text = action.get("lane"), action.get("text", "")
        text = raw_text.strip() if isinstance(raw_text, str) else None
        if (not isinstance(lane, str) or lane not in res["lanes"] or not isinstance(text, str)
                or not text or len(text) > 5000):
            raise ValueError("Choose a valid lane and enter an answer (up to 5,000 characters).")
        return base + ["answer"] + args + ["--lane", lane, "--text", text]
    if verb == "deployed":
        repo = action.get("repo")
        repos = (res["lanes"].get("dev") or {}).get("repos") or {}
        if (res["status"] != "Deploying" or not isinstance(repo, str) or repo not in repos
                or (res.get("release") or {}).get("repos", {}).get(repo) == "deployed"):
            raise ValueError("That repository is not part of this run.")
        release = res.get("release") or {}
        if release.get("deploy_order") == "ordered":
            sequence = release.get("sequence") or list(repos)
            next_repo = next(
                (name for name in sequence if release.get("repos", {}).get(name) != "deployed"),
                None,
            )
            if repo != next_repo:
                raise ValueError(f"Deployment order requires confirming {next_repo or 'the remaining release'} next.")
        return base + ["deployed"] + args + ["--repo", repo]
    if verb == "accept":
        if res["status"] != "Awaiting acceptance":
            raise ValueError("This work item is not ready for acceptance.")
        return base + ["accept"] + args
    raise ValueError("Unsupported workflow action.")


def confirm_human_action(action):
    ticket = valid_ticket(action.get("ticket"))
    run_number = action.get("run")
    if not isinstance(run_number, int) or isinstance(run_number, bool) or run_number < 1:
        raise ValueError("Run must be a positive integer.")
    text = action.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 5000:
        raise ValueError("A valid human recommendation is required.")
    recommendation = text.strip()
    result = analyze(ticket, run_number)
    if not any(item["who"].lower().startswith("human") and item["text"] == recommendation
               for item in result["next"]):
        raise ValueError("That human action is no longer recommended. Refresh the workflow status.")
    run = wf_state.Run(ticket, run_number)
    run.event("human", "human_action_confirmed", {"action": recommendation})
    run.decide(f"Human confirmed completion: {recommendation}")
    return {"confirmed": True, "ticket": ticket, "run": run_number}


def start_job(command, label):
    process = subprocess.Popen(
        command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
        **({"start_new_session": True} if os.name != "nt" else {}),
    )
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id, "label": label, "status": "running", "output": "",
        "returncode": None, "process": process, "cancel_requested": False,
    }
    with jobs_lock:
        jobs[job_id] = job

    def collect():
        try:
            for line in process.stdout:
                with jobs_lock:
                    job["output"] = (job["output"] + line)[-24000:]
            code = process.wait()
            with jobs_lock:
                job["returncode"] = code
                if job["cancel_requested"]:
                    job["status"] = "cancelled"
                else:
                    job["status"] = "succeeded" if code == 0 else "failed"
        except OSError as exc:
            with jobs_lock:
                job["status"] = "failed"
                job["output"] += f"\nUnable to collect command output: {exc}"

    threading.Thread(target=collect, daemon=True).start()
    return {"job": job_id, "label": label}


def public_job(job):
    return {key: value for key, value in job.items() if key not in ("process", "cancel_requested")}


def cancel_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise KeyError("Job not found.")
        if job["status"] != "running" or job["process"].poll() is not None:
            raise ValueError("This command has already finished.")
        job["status"] = "cancelling"
        job["cancel_requested"] = True
        process = job["process"]

    try:
        if os.name == "nt":
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            if not taskkill.is_file():
                raise OSError(f"Could not find process-tree terminator: {taskkill}")
            result = subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            )
            if result.returncode and process.poll() is None:
                raise OSError(result.stderr.strip() or result.stdout.strip() or "taskkill failed")
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
        return public_job(job)
    except (OSError, subprocess.SubprocessError) as exc:
        with jobs_lock:
            if process.poll() is None:
                job["status"] = "running"
                job["cancel_requested"] = False
        raise RuntimeError(f"Could not cancel command: {exc}") from exc


class PlaybookHandler(BaseHTTPRequestHandler):
    server_version = "WorkflowPlaybook/1.0"

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            try:
                body = PLAYBOOK.read_bytes()
            except OSError as exc:
                self.send_json({"error": f"Could not read the playbook page: {exc}"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/tickets":
            result = []
            base = ROOT / "tickets"
            for folder in sorted(base.iterdir(), key=lambda p: p.name.lower()) if base.is_dir() else []:
                if not folder.is_dir() or folder.name.startswith("_"):
                    continue
                try:
                    result.append(analyze(folder.name, None))
                except (wf_status.StatusError, OSError, ValueError) as exc:
                    result.append({"ticket": folder.name, "status": "Error", "detail": str(exc), "warnings": []})
            self.send_json({"tickets": result})
            return
        if parsed.path == "/api/status":
            query = parse_qs(parsed.query)
            try:
                ticket = valid_ticket(query.get("ticket", [""])[0])
                raw_run = query.get("run", [None])[0]
                run = int(raw_run) if raw_run else None
                self.send_json({"status": analyze(ticket, run)})
            except (ValueError, wf_status.StatusError, OSError) as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path[len("/api/jobs/"):]
            if not re.fullmatch(r"[a-f0-9]{32}", job_id):
                self.send_json({"error": "Job not found."}, 404)
                return
            with jobs_lock:
                job = jobs.get(job_id)
                self.send_json({"job": public_job(job)} if job else {"error": "Job not found."}, 200 if job else 404)
            return
        self.send_json({"error": "Not found."}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/action" and not re.fullmatch(r"/api/jobs/[a-f0-9]{32}/cancel", path):
            self.send_json({"error": "Not found."}, 404)
            return
        try:
            origin = self.headers.get("Origin")
            if origin:
                origin_host = urlparse(origin).hostname
                if origin_host not in ("127.0.0.1", "localhost"):
                    raise ValueError("Actions are accepted only from the local playbook.")
            if path != "/api/action":
                job_id = path[len("/api/jobs/"):-len("/cancel")]
                try:
                    result = cancel_job(job_id)
                except KeyError as exc:
                    self.send_json({"error": str(exc)}, 404)
                    return
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, 409)
                    return
                except RuntimeError as exc:
                    self.send_json({"error": str(exc)}, 500)
                    return
                self.send_json({"job": result})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 16000:
                raise ValueError("Request body must be between 1 and 16,000 bytes.")
            action = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(action, dict):
                raise ValueError("Action must be a JSON object.")
            if action.get("action") == "confirm-human":
                result = confirm_human_action(action)
                self.send_json(result)
                return
            command = command_for(action)
            label = (
                "Inspect & Adapt"
                if action.get("action") == "inspect-adapt"
                else str(action.get("action", "workflow action"))
            )
            result = start_job(command, label)
            self.send_json(result, 202)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, wf_status.StatusError, OSError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def log_message(self, fmt, *args):
        print(f"[playbook] {self.address_string()} {fmt % args}", flush=True)


def main():
    port = 8765
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            sys.exit("usage: python playbook/server.py [port 1-65535]")
    server = ThreadingHTTPServer(("127.0.0.1", port), PlaybookHandler)
    print(f"Workflow playbook ready at http://127.0.0.1:{port} (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping workflow playbook.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
