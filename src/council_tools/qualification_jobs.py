"""Detached local qualification jobs with durable exits and explicit resource claims.

This is an opt-in development supervisor, not a replacement for run-guard limits,
canonical test receipts, or production scheduling. A lost supervisor never releases
an admitted claim automatically. Commands are explicit argv, never shell strings.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


class JobRefused(ValueError):
    pass


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _directory(path):
    path = path.absolute()
    info = path.stat()
    if path.resolve(strict=True) != path or not path.is_dir() or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise JobRefused("job directory must be real, operator-owned and not writable by others")
    return path


def _save(path, value, *, new=False):
    # All calls occur inside the single manager transaction, except exclusive init.
    payload = _bytes(value)
    temporary = path if new else path.with_name(path.name + ".pending")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if not new:
            os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        # A pending file is preserved as ambiguous evidence; subsequent writes refuse.
        raise


def _load(path):
    if path.is_symlink() or not path.is_file():
        raise JobRefused("job evidence must be a regular file")
    return json.loads(path.read_bytes())


@contextmanager
def _transaction(root):
    root = _directory(root)
    fd = os.open(root / "manager.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield root
    finally:
        os.close(fd)


def _resources(value):
    if not isinstance(value, dict) or not value or any(
            not isinstance(k, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", k)
            or type(v) is not int or not 1 <= v <= 64 for k, v in value.items()):
        raise JobRefused("resources require named integer capacities from 1 to 64")
    return value


def initialize(root: Path, capacities: dict):
    _resources(capacities)
    root = _directory(root)
    with _transaction(root):
        _save(root / "manager.json", {"schemaVersion": 1, "capacities": capacities}, new=True)


def _process_identity(pid):
    try:
        # comm can contain spaces and parentheses; fields after its last ')' are stable.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {"pid": pid, "startTicks": fields[19],
                "bootId": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    except (FileNotFoundError, ProcessLookupError):
        return None


def _binding(request):
    cwd = Path(request["cwd"])
    command = request["argv"]
    if not cwd.is_absolute() or cwd.resolve(strict=True) != cwd or not cwd.is_dir():
        raise JobRefused("cwd must be a real absolute directory")
    if not isinstance(command, list) or not command or any(not isinstance(a, str) for a in command):
        raise JobRefused("argv must be an explicit nonempty string array")
    executable = Path(command[0])
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise JobRefused("executable must be an absolute executable file")
    if not isinstance(request["inputs"], list) or not request["inputs"]:
        raise JobRefused("explicit input files required")
    inputs = {}
    for name in request["inputs"]:
        path = Path(name)
        if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_file():
            raise JobRefused("input must be a real absolute file")
        if name in inputs:
            raise JobRefused("duplicate input")
        inputs[name] = {"sha256": _digest(path), "mode": path.stat().st_mode & 0o777}
    environment = request["environment"]
    if not isinstance(environment, dict) or any(
            not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
            or not isinstance(v, str) or "\0" in v for k, v in environment.items()):
        raise JobRefused("environment must contain explicit string variables")
    return {"inputs": inputs, "executableSha256": _digest(executable),
            "supervisorSha256": _digest(Path(__file__)), "uid": os.geteuid(),
            "gid": os.getegid(), "groups": sorted(os.getgroups()),
            "inheritedEnvironmentSha256": hashlib.sha256(_bytes(dict(os.environ))).hexdigest()}


def submit(root: Path, request: dict):
    if not isinstance(request, dict) or set(request) != {"programId", "argv", "cwd", "inputs", "environment", "resources"}:
        raise JobRefused("unexpected request fields")
    if not isinstance(request["programId"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", request["programId"]):
        raise JobRefused("programId required")
    _resources(request["resources"])
    binding = _binding(request)
    job_id = "job-" + uuid.uuid4().hex
    with _transaction(root) as root:
        capacities = _load(root / "manager.json")["capacities"]
        if any(k not in capacities or v > capacities[k] for k, v in request["resources"].items()):
            raise JobRefused("request exceeds configured resource capacity")
        state = {"schemaVersion": 1, "jobId": job_id, "state": "QUEUED",
                 "request": request, "binding": binding, "queuedAt": _now(),
                 "requestSha256": hashlib.sha256(_bytes(request)).hexdigest(),
                 "supervisor": None, "child": None, "nativeExit": None}
        path = root / (job_id + ".json")
        _save(path, state, new=True)
        with (root / (job_id + ".supervisor.log")).open("xb") as log:
            process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
                                        "worker", "--root", str(root), "--job-id", job_id],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       env=dict(os.environ), start_new_session=True, close_fds=True)
        state["supervisor"] = _process_identity(process.pid)
        _save(path, state)
    return state


def _states(root):
    return [_load(p) for p in sorted(root.glob("job-*.json"))]


def _available(state, states, capacities):
    used = Counter()
    for row in states:
        # No heartbeat timeout: an unknown orphan holds its admission until reconciled.
        if row["state"] in ("RUNNING", "UNKNOWN_EXECUTION", "UNKNOWN_DESCENDANTS"):
            used.update(row["request"]["resources"])
    return all(used[k] + v <= capacities[k] for k, v in state["request"]["resources"].items())


def _adopt_descendants():
    # Linux PR_SET_CHILD_SUBREAPER: orphaned grandchildren remain children of
    # this supervisor even when they create a new session/process group.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise JobRefused("cannot establish descendant custody")


def _remaining_children():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return []
        if pid == 0:
            break
    children = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[1]) == os.getpid():
                identity = _process_identity(int(path.name))
                if identity is not None:
                    children.append(identity)
        except (FileNotFoundError, ProcessLookupError):
            continue
    return children


def worker(root: Path, job_id: str):
    if not re.fullmatch(r"job-[0-9a-f]{32}", job_id):
        raise JobRefused("invalid job identifier")
    path = root / (job_id + ".json")
    while True:
        with _transaction(root):
            state = _load(path)
            if state["state"] != "QUEUED" or state["supervisor"] != _process_identity(os.getpid()):
                raise JobRefused("worker does not own queued job")
            capacities = _load(root / "manager.json")["capacities"]
            if _available(state, _states(root), capacities):
                current_binding = _binding(state["request"])
                if current_binding != state["binding"]:
                    state.update(state="REFUSED_INPUT_DRIFT", endedAt=_now(),
                                 driftFields=[key for key in current_binding
                                              if current_binding[key] != state["binding"][key]])
                    _save(path, state)
                    return 2
                state.update(state="RUNNING", startedAt=_now())
                _save(path, state)
                break
        time.sleep(0.25)
    request = state["request"]
    environment = dict(os.environ, **request["environment"])
    _adopt_descendants()
    try:
        with (root / (job_id + ".output.log")).open("xb") as log:
            child = subprocess.Popen(request["argv"], cwd=request["cwd"], env=environment,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     close_fds=True, start_new_session=True)
            with _transaction(root):
                state["child"] = _process_identity(child.pid)
                _save(path, state)
            native_exit = child.wait()
    except OSError as exc:
        # No native exit is invented when creation or logging failed.
        with _transaction(root):
            state.update(state="UNKNOWN_EXECUTION", error=str(exc), observedAt=_now())
            _save(path, state)
        return 2
    deadline = time.monotonic() + 1
    descendants = _remaining_children()
    while descendants and time.monotonic() < deadline:
        time.sleep(0.05)
        descendants = _remaining_children()
    with _transaction(root):
        state.update(state="UNKNOWN_DESCENDANTS" if descendants else "FINISHED",
                     remainingChildren=descendants, nativeExit=native_exit, endedAt=_now(),
                     outputSha256=_digest(root / (job_id + ".output.log")))
        _save(path, state)
    return 2 if descendants else 0


def report(root: Path):
    with _transaction(root):
        rows = _states(root)
        counts = Counter()
        programs = {}
        for row in rows:
            observed = row["state"]
            if observed in ("RUNNING", "QUEUED") and (
                    row["supervisor"] is None or
                    _process_identity(row["supervisor"]["pid"]) != row["supervisor"]):
                observed = "UNKNOWN_SUPERVISOR_LOST"
            row["observedState"] = observed
            counts[observed] += 1
            program = programs.setdefault(row["request"]["programId"], Counter())
            program[observed] += 1
            if row["state"] == "FINISHED" and row["nativeExit"] != 0:
                program["failedNativeExits"] += 1
        return {"schemaVersion": 1, "jobs": rows, "counts": dict(counts),
                "programs": {k: dict(v) for k, v in programs.items()},
                "authorizationEffect": False, "qualificationEffect": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "submit", "report", "worker"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--job-id")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            initialize(args.root, _load(args.spec))
            result = {"status": "INITIALIZED"}
        elif args.command == "submit":
            result = submit(args.root, _load(args.spec))
        elif args.command == "worker":
            return worker(args.root, args.job_id or "")
        else:
            result = report(args.root)
    except (ValueError, OSError, TypeError) as exc:
        sys.stderr.write(f"qualification job refused: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
