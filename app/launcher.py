"""
YODAW launcher / runtime supervisor.

One command runs the whole runtime:

    ./yodaw start          # or: python -m app.launcher start
    ./yodaw status
    ./yodaw stop
    ./yodaw restart
    ./yodaw logs [--follow]
    ./yodaw health

Architecture
------------
`start` spawns a detached supervisor daemon (new session, no terminal,
log fd only) and waits until the runtime answers its health probe.

The supervisor:

  - holds an exclusive flock on `<runtime_dir>/supervisor.pid`, so a
    second `start` can never double-spawn (singleton);
  - starts services in order and gates each on a readiness probe
    before moving on. Today there is exactly one service: `runtime`
    (`python -m app.runtime`), the production entrypoint that embeds
    API + coordinator + watchdog + outbox relay. The launcher therefore
    preserves the existing backend semantics verbatim: same command,
    same environment variables, same process behavior.
  - watches the service and respawns it with capped backoff when it
    dies (crash recovery);
  - refuses to start while another process already answers the health
    endpoint (no duplicate runtime);
  - on SIGTERM (from `yodaw stop`) drains the service gracefully:
    SIGTERM, wait, SIGKILL fallback; then removes its pidfile.

`status`, `stop`, `restart`, `logs`, `health` are thin clients: they
read the pidfile (its flock is the liveness flag), the machine-readable
state file, and the health endpoint. No service process is ever started
by anything other than the supervisor.

Layout (env-overridable, no user-specific paths):

    YODAW_RUNTIME_DIR           default <repo>/.yodaw
      supervisor.pid            singleton flock + supervisor pid
      state.json                supervisor + service state
      yodaw.log                 supervisor + service logs

The launcher itself uses only the Python standard library. The
runtime service is launched with the project interpreter
(YODAW_PYTHON, else <repo>/.venv/bin/python, else python3.12,
else python3).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - launcher is POSIX/macOS only
    fcntl = None

REPO_ROOT = Path(__file__).resolve().parent.parent

HEALTH_PATH = "/api/v1/health"
START_TIMEOUT_DEFAULT = 90      # seconds a service gets to become healthy
DRAIN_TIMEOUT = 25              # matches app.runtime's graceful drain window
BACKOFF_BASE = 1.5              # seconds, doubled per consecutive restart
BACKOFF_CAP = 30.0
POLL_INTERVAL = 0.5

# Services in dependency order. Each must pass its readiness probe
# before the next is started; a dead service is respawned with backoff.
# `runtime` embeds API + coordinator + watchdog + outbox relay, so this
# is the complete backend today.
SERVICES = ["runtime"]


def runtime_dir() -> Path:
    path = Path(os.environ.get("YODAW_RUNTIME_DIR") or REPO_ROOT / ".yodaw")
    path.mkdir(parents=True, exist_ok=True)
    return path


def host_port() -> tuple:
    host = os.environ.get("YODAW_HOST", "127.0.0.1")
    port = int(os.environ.get("YODAW_PORT", "8844"))
    return host, port


def health_url() -> str:
    host, port = host_port()
    return f"http://{host}:{port}{HEALTH_PATH}"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_python() -> str:
    """Interpreter used for the launcher daemon and the runtime service."""
    env = os.environ.get("YODAW_PYTHON", "").strip()
    if env:
        return env
    venv = REPO_ROOT / ".venv" / "bin" / "python"
    if venv.exists():
        return str(venv)
    for name in ("python3.12", "python3"):
        found = shutil.which(name)
        if found:
            return found
    return sys.executable


# ---------------------------------------------------------------- HTTP ---

def _http_open(url: str, timeout: float):
    """Proxyless GET so local health checks never route through HTTP_PROXY."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(url, timeout=timeout)


def probe_health(timeout: float = 2.0):
    """Returns (ok, payload_or_error)."""
    try:
        with _http_open(health_url(), timeout=timeout) as resp:
            raw = resp.read().decode() or "{}"
            data = json.loads(raw)
            if resp.status != 200:
                return False, f"http {resp.status}"
            if data.get("status") != "READY":
                return False, f"status={data.get('status')!r}"
            return True, data
    except Exception as exc:
        return False, str(exc)


# -------------------------------------------------------------- lock -----

def flock_exclusive_nonblock(fd: int) -> bool:
    if fcntl is None:
        raise RuntimeError("YODAW launcher requires flock (macOS/Linux)")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def try_lock_pidfile(path: Path):
    """Open the pidfile and take an exclusive flock. Returns the fd, or
    None when another live supervisor holds it."""
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
    if flock_exclusive_nonblock(fd):
        return fd
    os.close(fd)
    return None


def read_pidfile() -> int:
    path = runtime_dir() / "supervisor.pid"
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def supervisor_alive():
    """(alive, supervisor_pid). The pidfile's flock is the liveness flag:
    acquiring it means the holder is gone (stale entry)."""
    pid = read_pidfile()
    if pid == 0:
        return False, 0
    fd = try_lock_pidfile(runtime_dir() / "supervisor.pid")
    if fd is not None:
        os.close(fd)
        return False, 0
    return True, pid


def state_path() -> Path:
    return runtime_dir() / "state.json"


def read_state() -> dict:
    try:
        return json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}


def write_state(state: dict) -> None:
    state["updated_at"] = now()
    path = state_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, path)


def daemon_log(msg: str) -> None:
    with open(runtime_dir() / "yodaw.log", "a") as fh:
        fh.write(f"[supervisor] {now()} {msg}\n")


def _rm_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _cleanup_files() -> None:
    _rm_quiet(runtime_dir() / "supervisor.pid")
    _rm_quiet(state_path())


def cleanup_leftovers(state: dict) -> None:
    """SIGTERM, then SIGKILL, any recorded service pids (best effort).
    Used when the supervisor is already dead (crash / SIGKILL)."""
    for svc in state.get("services", {}).values():
        pid = svc.get("pid")
        if pid:
            _terminate_pid(pid, wait=3)


def _terminate_pid(pid: int, wait: float = DRAIN_TIMEOUT) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        # Reap our own child so a zombie does not stall the wait.
        try:
            wpid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            wpid = 0
        if wpid != 0:
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def backoff_seconds(restarts: int) -> float:
    return min(BACKOFF_BASE * (2 ** max(0, restarts - 1)), BACKOFF_CAP)


# ----------------------------------------------------------- supervisor ---

def _spawn_runtime() -> subprocess.Popen:
    logfile = runtime_dir() / "yodaw.log"
    out = open(logfile, "ab")
    try:
        return subprocess.Popen(
            [resolve_python(), "-m", "app.runtime"],
            cwd=str(REPO_ROOT),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    finally:
        out.close()


def _reap(pid: int):
    """waitpid WNOHANG; returns exit code or None while still running."""
    try:
        wpid, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return 0
    if wpid == 0:
        return None
    return os.waitstatus_to_exitcode(status)


def _ensure_pidfile(fd: int) -> int:
    """The pidfile is the daemon's identity: if anything removed it
    while the daemon lives, re-create it so stop/status can find us
    again."""
    if (runtime_dir() / "supervisor.pid").exists():
        return fd
    try:
        os.close(fd)
    except OSError:
        pass
    new_fd = try_lock_pidfile(runtime_dir() / "supervisor.pid")
    if new_fd is None:
        return fd
    os.ftruncate(new_fd, 0)
    os.write(new_fd, str(os.getpid()).encode())
    daemon_log("pidfile was missing; re-created")
    return new_fd


def _daemonize() -> int:
    """Double-fork so the supervisor reparents to launchd immediately
    (no controlling terminal, immune to the caller's session/group
    ending). The grandchild then runs the real supervisor loop."""
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    return supervise()


def supervise() -> int:
    """Detached daemon body (invoked as `_supervise` by `start`)."""
    pidfile = runtime_dir() / "supervisor.pid"

    fd = try_lock_pidfile(pidfile)
    if fd is None:
        daemon_log("another supervisor already holds the pidfile; exiting")
        return 1
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())

    services = {}
    for name in SERVICES:
        services[name] = {
            "pid": None,
            "status": "starting",   # starting | running | restarting
            "started_at": None,
            "restarts": 0,
            "last_error": None,
        }
    state = {
        "instance_id": uuid.uuid4().hex[:12],
        "supervisor_pid": os.getpid(),
        "python": resolve_python(),
        "started_at": now(),
        "host": host_port()[0],
        "port": host_port()[1],
        "services": services,
    }
    write_state(state)
    daemon_log(
        f"supervisor {os.getpid()} started (instance {state['instance_id']})"
    )

    running = {"flag": False}  # shutting down

    def _handle_signal(signum, _frame):
        if not running["flag"]:
            running["flag"] = True
            daemon_log(f"received signal {signum}; draining")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    svc = services["runtime"]
    respawn_at = 0.0
    last_state_write = 0.0
    last_log = {}

    while not running["flag"]:
        # Reap a dead service and schedule its respawn.
        if svc["pid"] is not None:
            rc = _reap(svc["pid"])
            if rc is not None:
                svc["restarts"] += 1
                svc["last_error"] = f"exit rc={rc}"
                svc["status"] = "restarting"
                svc["started_at"] = None
                daemon_log(
                    f"runtime exited rc={rc}; respawning in "
                    f"{backoff_seconds(svc['restarts']):.1f}s "
                    f"(restart {svc['restarts']})"
                )
                respawn_at = time.time() + backoff_seconds(svc["restarts"])
                svc["pid"] = None

        # Respawn when the backoff has elapsed.
        if svc["pid"] is None and time.time() >= respawn_at:
            # Never spawn while another process answers the health
            # endpoint: our previous child is dead (reaped above), so
            # an answerer can only be a foreign runtime.
            served, _ = probe_health(timeout=1.0)
            if served:
                daemon_log(
                    "health endpoint already answered by another process; "
                    "refusing to start a duplicate; exiting"
                )
                os.close(fd)
                _cleanup_files()
                return 1
            try:
                proc = _spawn_runtime()
            except OSError as exc:
                svc["last_error"] = f"spawn failed: {exc}"
                respawn_at = time.time() + backoff_seconds(svc["restarts"] + 1)
                daemon_log(f"runtime spawn failed: {exc}")
            else:
                svc["pid"] = proc.pid
                svc["started_at"] = now()
                svc["status"] = "starting"
                daemon_log(f"runtime started pid={proc.pid}")

        # Readiness gate: starting -> running once the probe passes.
        if svc["pid"] is not None and svc["status"] == "starting":
            ok, payload = probe_health()
            if ok:
                svc["status"] = "running"
                svc["last_error"] = None
                daemon_log(f"runtime healthy: {health_url()}")
            elif payload != last_log.get("starting"):
                daemon_log(f"runtime not ready yet ({payload})")
                last_log["starting"] = payload

        if time.time() - last_state_write >= 1.0:
            fd = _ensure_pidfile(fd)
            write_state(state)
            last_state_write = time.time()

        time.sleep(POLL_INTERVAL)

    # Graceful shutdown.
    pid = svc.get("pid")
    if pid is not None:
        rc = _reap(pid)
        if rc is None:
            daemon_log(f"sending SIGTERM to runtime pid={pid}")
            _terminate_pid(pid)
            daemon_log(f"runtime pid={pid} stopped")
        svc["pid"] = None
        write_state(state)

    os.close(fd)
    _cleanup_files()
    daemon_log(f"supervisor {os.getpid()} stopped cleanly")
    return 0


# ---------------------------------------------------------------- CLI -----

def cmd_start(args) -> int:
    alive, pid = supervisor_alive()
    if alive:
        print(f"YODAW: already running (supervisor pid {pid}); see `yodaw status`")
        return 0

    ok, _ = probe_health(timeout=1.5)
    if ok:
        print(
            f"YODAW: {health_url()} is already served by another process; "
            "refusing to start a duplicate. Stop it first.",
            file=sys.stderr,
        )
        return 1

    _cleanup_files()

    logfile = runtime_dir() / "yodaw.log"
    with open(logfile, "ab") as out:
        subprocess.Popen(
            [resolve_python(), "-m", "app.launcher", "_daemonize"],
            cwd=str(REPO_ROOT),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    # Wait for the daemon to take ownership (pidfile + flock) and to
    # declare the runtime running. The daemon is double-forked, so
    # there is no direct child to poll; absence of a pidfile here
    # just means the daemon has not booted yet.
    deadline = time.time() + args.timeout
    healthy = False
    while time.time() < deadline:
        alive, spid = supervisor_alive()
        if alive:
            svc = read_state().get("services", {}).get("runtime", {})
            if svc.get("status") == "running":
                healthy = True
                pid = spid
                break
        time.sleep(POLL_INTERVAL)

    if not healthy:
        print(
            f"YODAW: runtime not healthy within {args.timeout}s; "
            f"check `yodaw logs` ({logfile})",
            file=sys.stderr,
        )
        return 1

    print(f"YODAW: started (supervisor pid {pid}, {health_url()})")
    return 0


def cmd_status(args) -> int:
    alive, pid = supervisor_alive()
    state = read_state()
    ok, payload = probe_health(timeout=2.0)

    if args.json:
        doc = {
            "running": alive,
            "supervisor_pid": pid if alive else None,
            "instance_id": state.get("instance_id"),
            "started_at": state.get("started_at"),
            "host": state.get("host"),
            "port": state.get("port"),
            "services": state.get("services", {}),
            "health": {"ok": ok, "endpoint": health_url(), "detail": payload if ok else None,
                       "error": None if ok else payload},
        }
        print(json.dumps(doc, indent=2))
        return 0 if alive and ok and all(
            s.get("status") == "running" for s in state.get("services", {}).values()
        ) else 1

    if not alive:
        print("YODAW: not running")
        leftovers = [
            (name, s.get("pid")) for name, s in state.get("services", {}).items()
            if s.get("pid")
        ]
        if leftovers:
            print("  leftover service pids from a dead supervisor:")
            for name, lpid in leftovers:
                print(f"    {name}: pid {lpid}  (run `yodaw stop` to reap)")
        return 1

    print(f"YODAW runtime: UP (supervisor pid {pid})")
    print(
        f"{'service':<10} {'pid':>8}  {'state':<10} {'restarts':>8}  uptime"
    )
    for name, svc in sorted(state.get("services", {}).items()):
        uptime = ""
        if svc.get("started_at"):
            try:
                start = datetime.fromisoformat(svc["started_at"])
                uptime = f"{max(0, int(time.time() - start.timestamp()))}s"
            except (ValueError, TypeError):
                uptime = "?"
        err = f"  ({svc.get('last_error')})" if svc.get("last_error") else ""
        print(
            f"{name:<10} {svc.get('pid') or '-':>8}  "
            f"{svc.get('status', '?'):<10} {svc.get('restarts', 0):>8}  "
            f"{uptime or '-':<6}{err}"
        )
    if ok:
        print(
            f"  health: {health_url()} -> READY "
            f"(auth={payload.get('auth')}, profile={payload.get('profile')}, "
            f"backend={payload.get('storage_backend')})"
        )
    else:
        print(f"  health: {health_url()} -> Unreachable ({payload})")

    all_running = all(
        s.get("status") == "running" for s in state.get("services", {}).values()
    )
    return 0 if (all_running and ok) else 1


def cmd_stop(args) -> int:
    alive, pid = supervisor_alive()
    state = read_state()

    if alive:
        daemon_log(f"stop requested; signaling supervisor {pid}")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.time() + args.timeout
        while time.time() < deadline and supervisor_alive()[0]:
            time.sleep(0.2)
        if supervisor_alive()[0]:
            print(f"YODAW: supervisor {pid} did not exit; forcing", file=sys.stderr)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            cleanup_leftovers(state)
    else:
        # Dead supervisor: reap any orphaned services it recorded.
        cleanup_leftovers(state)

    _cleanup_files()
    print("YODAW: stopped")
    return 0


def cmd_restart(args) -> int:
    # `restart` may carry a stop --timeout; fall back to the default.
    args.timeout = getattr(args, "timeout", START_TIMEOUT_DEFAULT + 15)
    rc = cmd_stop(args)
    if rc != 0:
        return rc
    time.sleep(0.5)
    return cmd_start(args)


def cmd_logs(args) -> int:
    path = runtime_dir() / "yodaw.log"
    if not path.exists():
        print("no logs yet; start YODAW first")
        return 0

    if args.follow:
        size = 0
        while True:
            current = path.stat().st_size
            if current > size:
                with open(path, "rb") as fh:
                    fh.seek(size)
                    sys.stdout.buffer.write(fh.read())
                sys.stdout.flush()
                size = current
            time.sleep(0.5)
        return 0

    lines = path.read_text().splitlines()
    for line in lines[-args.tail:]:
        print(line)
    return 0


def cmd_health(_args) -> int:
    ok, payload = probe_health()
    if ok:
        print(json.dumps(payload, indent=2))
        return 0
    print(f"YODAW: unhealthy: {payload}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yodaw",
        description="YODAW launcher / runtime supervisor",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="start the runtime (idempotent)")
    p.add_argument(
        "--timeout", type=int, default=START_TIMEOUT_DEFAULT,
        help=f"seconds to wait for a healthy runtime (default {START_TIMEOUT_DEFAULT})",
    )
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("status", help="runtime + service status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("stop", help="graceful shutdown")
    p.add_argument(
        "--timeout", type=int, default=START_TIMEOUT_DEFAULT + 15
    )
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("restart", help="stop, then start")
    p.add_argument(
        "--timeout", type=int, default=START_TIMEOUT_DEFAULT + 15
    )
    p.set_defaults(func=cmd_restart)

    p = sub.add_parser("logs", help="read the runtime log")
    p.add_argument("-n", "--tail", type=int, default=50)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_logs)

    sub.add_parser("health", help="print the health endpoint").set_defaults(
        func=cmd_health
    )
    return parser


def main(argv=None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)

    # Internal daemon entrypoints: must bypass argparse's subparser
    # whitelist (the daemon is not a user-facing command).
    if args_list and args_list[0] == "_daemonize":
        return _daemonize()
    if args_list and args_list[0] == "_supervise":
        return supervise()

    parser = build_parser()
    args = parser.parse_args(args_list)

    if fcntl is None:
        print("YODAW launcher requires flock (macOS/Linux)", file=sys.stderr)
        return 1

    # Keep sys.path importable regardless of invocation cwd.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())