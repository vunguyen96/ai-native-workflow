#!/usr/bin/env python3
"""Write side of the orchestrator: manifest, events, decisions and leases.

Agents never use this. Lane workers (scripts/wf_run.py), the demo driver and humans do.
Everything that changes a status goes through here, so there is one place that says how.
The read side stays in scripts/wf_status.py, which never writes.
"""
import contextlib
import json
import os
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import yaml
except ImportError:
    sys.exit("error: PyYAML is required. Install it with: python -m pip install -r requirements.txt")
import wf_status as S  # noqa: E402

OK = S.OK
MANIFEST_HEAD = "# Written by the orchestrator side (scripts/wf_state.py). Agents never edit this file.\n"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def short_hash(path):
    return S.sha256_path(path)[:8]


def latest_mtime(path):
    """Newest modification time of a file or of any file below a folder."""
    if path.is_dir():
        times = [f.stat().st_mtime for f in path.rglob("*") if f.is_file()]
        return max(times) if times else 0.0
    return path.stat().st_mtime if path.exists() else 0.0


class Run:
    def __init__(self, ticket, run, root=None):
        self.root = Path(root) if root else S.ROOT
        self.ticket, self.run = ticket, int(run)
        self.dir = self.root / "tickets" / ticket / f"run-{self.run}"
        if not (self.dir / "manifest.yaml").exists():
            raise S.StatusError(f"{self.dir}/manifest.yaml not found")
        self.art = self.dir / "artifacts"

    # ---- file lock (short critical sections only)
    @contextlib.contextmanager
    def lock(self, name="manifest", wait=30.0):
        d = self.dir / ".locks"
        d.mkdir(exist_ok=True)
        p = d / f"{name}.lock"
        end = time.time() + wait
        while True:
            try:
                fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - p.stat().st_mtime > 60:   # a crashed holder
                        p.unlink()
                        continue
                except OSError:
                    pass
                if time.time() > end:
                    raise TimeoutError(f"could not get the {name} lock in {wait:.0f}s ({p})")
                time.sleep(0.05)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                p.unlink()

    # ---- manifest
    def load(self):
        for attempt in range(6):
            try:
                return yaml.safe_load((self.dir / "manifest.yaml").read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                if attempt == 5:
                    raise
                time.sleep(0.1)

    def update(self, fn):
        """Read-modify-write the manifest under the lock. fn(manifest) changes it in place."""
        with self.lock():
            m = self.load()
            fn(m)
            text = yaml.safe_dump(m, sort_keys=False, default_flow_style=None, width=120)
            tmp = self.dir / "manifest.yaml.tmp"
            tmp.write_text(MANIFEST_HEAD + text, encoding="utf-8", newline="\n")
            for attempt in range(20):          # a reader may hold the file open for a moment on Windows
                try:
                    os.replace(tmp, self.dir / "manifest.yaml")
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.05)
        return m

    # ---- events and decisions (append-only)
    def event(self, actor, name, detail=None):
        line = f"{now()}\t{actor}\t{name}\t{json.dumps(detail or {}, sort_keys=True)}\n"
        with open(self.dir / "events.log", "a", encoding="utf-8", newline="\n") as f:
            f.write(line)

    def events(self):
        return S.read_events(self.dir / "events.log")

    def decide(self, text):
        with open(self.dir / "decisions.md", "a", encoding="utf-8", newline="\n") as f:
            f.write(f"\n- {datetime.now().date().isoformat()} - {text}\n")

    # ---- artifacts
    def path_of(self, key, repo=None):
        return self.art / "changes" / repo if repo else self.art / key

    def mark(self, key, status, inputs=None, approved_by=None, repo=None, version=None):
        """Set an artifact's status and record its hash and the hashes of the inputs it was built from."""
        def fn(m):
            arts = m.setdefault("artifacts", {})
            if repo:
                ch = arts.setdefault("changes", {"owner": "dev", "status": "missing"})
                e = ch.setdefault("per_repo", {}).get(repo) or {}
                path = S.artifact_path(self.dir, f"changes/{repo}")
            else:
                e = arts.get(key) or {"owner": "system"}
                path = S.artifact_path(self.dir, key)
            e["status"] = status
            if version:
                e["version"] = version
            if path is not None and path.exists():
                e["hash"] = short_hash(path)
            else:
                e.pop("hash", None)
            if inputs is not None:
                e["inputs"] = {
                    i: short_hash(input_path)
                    for i in inputs
                    if (input_path := S.artifact_path(self.dir, i)) is not None and input_path.exists()
                }
            if approved_by:
                e["approved_by"] = approved_by
            elif status != "approved":
                e.pop("approved_by", None)
            if repo:
                ch["per_repo"][repo] = e
                vals = [x.get("status") for x in ch["per_repo"].values()]
                ch["status"] = "ready" if vals and all(v in OK for v in vals) else "draft"
            else:
                arts[key] = e
        self.update(fn)

    def set_lane(self, lane, status, repo=None):
        def fn(m):
            lanes = m.setdefault("lanes", {})
            ent = lanes.get(lane) if isinstance(lanes.get(lane), dict) else {}
            if repo:
                ent.setdefault("repos", {})[repo] = status
            else:
                ent["status"] = status
                if status != "waiting":
                    ent.pop("retry_after_recheck", None)
            lanes[lane] = ent
        self.update(fn)

    # ---- leases: one writer per lane instance; an expired lease returns the work to the queue
    def acquire(self, unit, minutes):
        d = self.dir / "leases"
        d.mkdir(exist_ok=True)
        p = d / f"{unit}.json"
        for _ in range(2):
            try:
                fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    info = json.loads(p.read_text(encoding="utf-8"))
                    expired = datetime.fromisoformat(info["expires"]) < datetime.now(timezone.utc)
                except (OSError, ValueError, KeyError):
                    expired = True
                if not expired:
                    return False
                with contextlib.suppress(OSError):
                    p.unlink()
                self.event("orchestrator", "lease_expired", {"unit": unit})
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"unit": unit, "holder": f"{socket.gethostname()}:{os.getpid()}", "started": now(),
                           "expires": (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")}, f)
            return True
        return False

    def release(self, unit):
        with contextlib.suppress(OSError):
            (self.dir / "leases" / f"{unit}.json").unlink()

    def attempts_done(self, lane, repo=""):
        """How many sessions of this lane instance have finished (any outcome). The next attempt is this + 1."""
        return sum(1 for e in self.events()
                   if e["event"] == "lane_finished" and e["detail"].get("lane") == lane
                   and (e["detail"].get("repo") or "") == (repo or ""))

    def attempts_in_cycle(self, lane, repo=""):
        """How many sessions finished since this lane instance was last reopened."""
        events = self.events()
        start = 0
        for index, event in enumerate(events):
            if event["event"] == "lane_reopened" and event["detail"].get("lane") == lane \
                    and (event["detail"].get("repo") or "") == (repo or ""):
                start = index + 1
        return sum(1 for event in events[start:]
                   if event["event"] == "lane_finished" and event["detail"].get("lane") == lane
                   and (event["detail"].get("repo") or "") == (repo or ""))

    def last_outcome(self, lane, repo=""):
        """Outcome of the most recent finished session of this lane instance: ok | incomplete | crashed | None."""
        out = None
        for e in self.events():
            if e["event"] == "lane_finished" and e["detail"].get("lane") == lane and (e["detail"].get("repo") or "") == (repo or ""):
                out = e["detail"].get("outcome")
        return out
