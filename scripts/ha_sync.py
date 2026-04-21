#!/usr/bin/env python3
"""
Brother Agent — Offline multi-agent data mirroring.

Mirrors data between independent Hermes Agent nodes via rsync.
No merging, no rebuilds — just file-level copy into brother directories.

Usage:
    ha_sync.py add <name> --host <host> [--desc <description>]
    ha_sync.py sync <name>
    ha_sync.py sync --all
    ha_sync.py status [<name>]
    ha_sync.py list
    ha_sync.py remove <name>
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────

HERMES_DIR = Path(os.environ.get("HERMES_DIR", "~/.hermes")).expanduser()
BROTHERS_DIR = HERMES_DIR / "brothers"
NODES_FILE = BROTHERS_DIR / "nodes.yaml"
SCRIPT_DIR = Path(__file__).resolve().parent
LOCAL_HOSTNAME = os.uname().nodename

SSH_TIMEOUT = 10
RSYNC_TIMEOUT = 120

# Watch state file: tracks last known reachability per brother
WATCH_STATE_FILE = BROTHERS_DIR / "watch_state.json"

# ── Helpers ────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = f"[{ts}] [{level}] "
    if level == "ERROR":
        print(prefix + msg, file=sys.stderr)
    elif level != "DEBUG":
        print(prefix + msg)


def run(cmd: str, check: bool = True, timeout: int = None) -> subprocess.CompletedProcess:
    """Run a shell command."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, check=False
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"Command failed (rc={result.returncode}): {cmd}\nstderr: {result.stderr[:500]}"
            )
        return result
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out after {timeout}s: {cmd}")


def ssh_cmd(host: str, cmd: str, timeout: int = SSH_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a command on remote host via SSH."""
    full = f"ssh -o ConnectTimeout={SSH_TIMEOUT} -o BatchMode=yes {host} '{cmd}'"
    return run(full, timeout=timeout, check=False)


def ssh_reachable(host: str) -> bool:
    """Check if host is reachable via SSH."""
    try:
        r = ssh_cmd(host, "echo ok", timeout=5)
        return r.returncode == 0 and "ok" in r.stdout
    except Exception:
        return False


def rsync_push(src: str, dst_host: str, dst_path: str, timeout: int = RSYNC_TIMEOUT):
    """rsync local src to remote dst."""
    run(
        f"rsync -avz --update --timeout={timeout} {src} {dst_host}:{dst_path}",
        check=False, timeout=timeout + 30
    )


def rsync_pull(src_host: str, src_path: str, dst: str, timeout: int = RSYNC_TIMEOUT):
    """rsync remote src to local dst."""
    run(
        f"rsync -avz --update --timeout={timeout} {src_host}:{src_path} {dst}",
        check=False, timeout=timeout + 30
    )


# ── Nodes Registry ─────────────────────────────────────────────

def load_nodes() -> dict:
    """Load nodes registry from nodes.yaml."""
    if not NODES_FILE.exists():
        return {"nodes": {}}
    try:
        import yaml
        with open(NODES_FILE, "r") as f:
            data = yaml.safe_load(f)
        return data if data and "nodes" in data else {"nodes": {}}
    except ImportError:
        # Fallback: try JSON if pyyaml not available
        json_file = BROTHERS_DIR / "nodes.json"
        if json_file.exists():
            with open(json_file, "r") as f:
                return json.load(f)
        return {"nodes": {}}
    except Exception:
        return {"nodes": {}}


def save_nodes(data: dict):
    """Save nodes registry."""
    BROTHERS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        import yaml
        with open(NODES_FILE, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True)
    except ImportError:
        json_file = BROTHERS_DIR / "nodes.json"
        with open(json_file, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def get_node(name: str) -> dict:
    """Get a node's config by name."""
    data = load_nodes()
    node = data.get("nodes", {}).get(name)
    if not node:
        print(f"ERROR: Brother '{name}' not registered. Use 'ha_sync.py add {name} --host <host>'")
        sys.exit(1)
    return node


# ── Memory Snapshot ────────────────────────────────────────────

def backup_memory_local() -> Path:
    """Create a local memory_store.db snapshot using conn.backup()."""
    src = HERMES_DIR / "memory_store.db"
    dst = HERMES_DIR / "memory_store.snapshot.db"
    if not src.exists():
        raise FileNotFoundError("No memory_store.db found")
    dst.unlink(missing_ok=True)
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    src_conn.backup(dst_conn)
    dst_conn.close()
    src_conn.close()
    return dst


# ── Commands ───────────────────────────────────────────────────

def cmd_add(args):
    """Register a new brother node."""
    name = args.name
    host = args.host
    desc = getattr(args, "desc", "") or ""

    if not name or not host:
        print("ERROR: --name and --host are required")
        sys.exit(1)

    # Validate name: alphanumeric, dashes, underscores only
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        print(f"ERROR: Invalid name '{name}'. Use alphanumeric, dashes, underscores only.")
        sys.exit(1)

    data = load_nodes()
    if name in data.get("nodes", {}):
        print(f"WARNING: Brother '{name}' already registered. Updating.")

    data.setdefault("nodes", {})[name] = {
        "host": host,
        "user": getattr(args, "user", "") or "",
        "description": desc,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    save_nodes(data)

    # Create brother directory
    brother_dir = BROTHERS_DIR / name
    brother_dir.mkdir(parents=True, exist_ok=True)
    (brother_dir / "sessions").mkdir(exist_ok=True)
    (brother_dir / "skills").mkdir(exist_ok=True)

    print(f"Registered brother '{name}' → {host}")
    print(f"  Directory: {brother_dir}")
    print(f"  Run 'ha_sync.py sync {name}' to synchronize.")


def cmd_sync(args):
    """Sync data with a brother node."""
    if getattr(args, "all", False):
        data = load_nodes()
        names = list(data.get("nodes", {}).keys())
        if not names:
            print("No brothers registered. Use 'ha_sync.py add' first.")
            return
        for name in names:
            print(f"\n{'='*50}")
            print(f"Syncing: {name}")
            print(f"{'='*50}")
            _sync_one(name)
        return

    name = args.name
    if not name:
        print("ERROR: Specify <name> or use --all")
        sys.exit(1)
    _sync_one(name)


def _sync_one(name: str):
    """Sync with a single brother node."""
    node = get_node(name)
    host = node["host"]
    quiet = "--quiet" in sys.argv

    # [0] Connectivity check
    if not ssh_reachable(host):
        if not quiet:
            print(f"SKIP: {name} ({host}) unreachable")
        return

    log(f"Syncing with {name} ({host})")
    start = time.time()

    # Determine remote hermes dir
    r = ssh_cmd(host, "echo $HOME", timeout=SSH_TIMEOUT)
    remote_home = r.stdout.strip()
    remote_hermes = f"{remote_home}/.hermes"
    remote_brother_dir = f"{remote_hermes}/brothers/{LOCAL_HOSTNAME}"

    # Ensure remote directories exist
    ssh_cmd(host, f"mkdir -p {remote_brother_dir}/sessions {remote_brother_dir}/skills",
            timeout=SSH_TIMEOUT)

    # Ensure local brother directories exist
    local_brother = BROTHERS_DIR / name
    local_brother.mkdir(parents=True, exist_ok=True)
    (local_brother / "sessions").mkdir(exist_ok=True)
    (local_brother / "skills").mkdir(exist_ok=True)

    # ── [1] Push: WSL → remote brother dir ──

    print("  [1/5] Pushing sessions...")
    rsync_push(
        f"{HERMES_DIR}/sessions/",
        host, f"{remote_brother_dir}/sessions/"
    )

    print("  [2/5] Pushing state.db + memory + config files...")
    # state.db
    state_db = HERMES_DIR / "state.db"
    if state_db.exists():
        rsync_push(str(state_db), host, f"{remote_brother_dir}/state.db")

    # memory_store.db snapshot
    mem_db = HERMES_DIR / "memory_store.db"
    if mem_db.exists():
        try:
            snapshot = backup_memory_local()
            rsync_push(str(snapshot), host, f"{remote_brother_dir}/memory_store.db")
        except Exception as e:
            log(f"Memory snapshot push failed: {e}", "ERROR")

    # MEMORY.md / USER.md (upgraded to memories/ subdir)
    memories_dir = HERMES_DIR / "memories"
    for f in ["MEMORY.md", "USER.md"]:
        src = memories_dir / f if (memories_dir / f).exists() else HERMES_DIR / f
        if src.exists():
            rsync_push(str(src), host, f"{remote_brother_dir}/{f}")

    # skills
    print("  [3/5] Pushing skills...")
    skills_dir = HERMES_DIR / "skills"
    if skills_dir.exists():
        rsync_push(f"{skills_dir}/", host, f"{remote_brother_dir}/skills/")

    # ── [2] Pull: remote → local brother dir ──

    print("  [4/5] Pulling data from remote...")
    # sessions
    rsync_pull(host, f"{remote_hermes}/sessions/", f"{local_brother}/sessions/")

    # state.db
    rsync_pull(host, f"{remote_hermes}/state.db", str(local_brother / "state.db"))

    # memory_store.db via snapshot
    snapshot_script = SCRIPT_DIR / "ha_snapshot.py"
    if snapshot_script.exists():
        remote_tmp = "/tmp/ha_snapshot.py"
        run(f"scp -o ConnectTimeout={SSH_TIMEOUT} {snapshot_script} {host}:{remote_tmp}",
            check=False, timeout=RSYNC_TIMEOUT)
        ssh_cmd(host, f"python3 {remote_tmp} --hermes-dir {remote_hermes}", timeout=60)
        rsync_pull(host, f"{remote_hermes}/memory_store.snapshot.db",
                   str(local_brother / "memory_store.db"))
        ssh_cmd(host, f"rm -f {remote_tmp}", timeout=SSH_TIMEOUT)

    # MEMORY.md / USER.md (upgraded to memories/ subdir)
    for f in ["MEMORY.md", "USER.md"]:
        # check new path first (memories/), fallback to old path (hermes root)
        for sub in ["memories", "."]:
            remote_path = f"{remote_hermes}/{sub}/{f}"
            r = ssh_cmd(host, f"test -f {remote_path} && echo exists || echo missing",
                        timeout=SSH_TIMEOUT)
            if "exists" in r.stdout:
                rsync_pull(host, remote_path, str(local_brother / f))
                break

    # skills
    print("  [5/5] Pulling skills...")
    rsync_pull(host, f"{remote_hermes}/skills/", f"{local_brother}/skills/")

    # Update last sync timestamp
    node["last_sync"] = datetime.now(timezone.utc).isoformat()
    data = load_nodes()
    data.setdefault("nodes", {})[name] = node
    save_nodes(data)

    elapsed = time.time() - start
    print(f"\n  Sync complete: {name} ({elapsed:.1f}s)")


def cmd_status(args):
    """Show status of local and remote nodes."""
    data = load_nodes()
    nodes = data.get("nodes", {})
    name = getattr(args, "name", None)

    print(f"=== Brother Agent Status ===")
    print(f"  Local hostname: {LOCAL_HOSTNAME}")
    print(f"  Brothers dir:   {BROTHERS_DIR}")
    print()

    if name:
        names = [name]
    else:
        names = list(nodes.keys())

    if not names:
        print("  No brothers registered. Use 'ha_sync.py add <name> --host <host>'")
        return

    for n in names:
        node = nodes.get(n)
        if not node:
            print(f"  [{n}] NOT REGISTERED")
            continue

        host = node.get("host", "?")
        desc = node.get("description", "")
        last_sync = node.get("last_sync", "never")

        print(f"  [{n}] {host}" + (f" — {desc}" if desc else ""))

        # Local brother directory stats
        bdir = BROTHERS_DIR / n
        if bdir.exists():
            sessions = list((bdir / "sessions").glob("session_*.json")) if (bdir / "sessions").exists() else []
            state_db = bdir / "state.db"
            mem_db = bdir / "memory_store.db"
            print(f"    Local mirror:")
            print(f"      Sessions:   {len(sessions)} files")
            print(f"      State DB:   {'%.1f KB' % (state_db.stat().st_size/1024) if state_db.exists() else 'MISSING'}")
            print(f"      Memory DB:  {'%.1f KB' % (mem_db.stat().st_size/1024) if mem_db.exists() else 'MISSING'}")
        else:
            print(f"    Local mirror: EMPTY (never synced)")

        # Remote connectivity
        if ssh_reachable(host):
            print(f"    SSH:         REACHABLE")
            # Quick remote stats
            try:
                r = ssh_cmd(host,
                    f"ls {host_hermes_path(host)}/sessions/session_*.json 2>/dev/null | wc -l",
                    timeout=SSH_TIMEOUT)
                # fallback - just show reachable
                print(f"    Last sync:   {last_sync}")
            except Exception:
                print(f"    Last sync:   {last_sync}")
        else:
            print(f"    SSH:         UNREACHABLE")
            print(f"    Last sync:   {last_sync}")
        print()


def host_hermes_path(host: str) -> str:
    """Get remote hermes path (cached per call)."""
    r = ssh_cmd(host, "echo $HOME", timeout=SSH_TIMEOUT)
    return f"{r.stdout.strip()}/.hermes"


def cmd_list(args):
    """List all registered brothers."""
    data = load_nodes()
    nodes = data.get("nodes", {})
    if not nodes:
        print("No brothers registered.")
        return
    print(f"{'Name':<15} {'Host':<20} {'Description':<30} {'Last Sync'}")
    print("-" * 85)
    for name, node in nodes.items():
        print(f"{name:<15} {node.get('host',''):<20} "
              f"{node.get('description',''):<30} {node.get('last_sync','never')}")


def cmd_remove(args):
    """Remove a brother registration."""
    name = args.name
    data = load_nodes()
    if name not in data.get("nodes", {}):
        print(f"Brother '{name}' not found.")
        return
    del data["nodes"][name]
    save_nodes(data)

    # Remove local brother directory
    bdir = BROTHERS_DIR / name
    if bdir.exists():
        shutil.rmtree(bdir)
        print(f"Removed directory: {bdir}")

    print(f"Brother '{name}' removed.")


# ── Watch (edge-triggered auto-sync) ───────────────────────────

def load_watch_state() -> dict:
    """Load watch state: {name: {"reachable": bool, "changed_at": iso}}"""
    if WATCH_STATE_FILE.exists():
        try:
            with open(WATCH_STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_watch_state(state: dict):
    """Persist watch state."""
    BROTHERS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = WATCH_STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(WATCH_STATE_FILE)  # atomic


def cmd_watch(args):
    """Edge-triggered watch: sync only on unreachable→reachable transition.

    Designed for cron. Outputs nothing unless a sync is triggered.
    """
    data = load_nodes()
    nodes = data.get("nodes", {})
    if not nodes:
        return

    watch_state = load_watch_state()
    now_iso = datetime.now(timezone.utc).isoformat()
    state_changed = False

    for name, node in nodes.items():
        host = node["host"]
        prev = watch_state.get(name, {})
        was_reachable = prev.get("reachable", False)
        is_reachable = ssh_reachable(host)

        if is_reachable and not was_reachable:
            # Rising edge: brother just came online
            log(f"MEET detected: {name} ({host}) came online, syncing...")
            _sync_one(name)
            state_changed = True

        # Update state
        new_entry = {"reachable": is_reachable, "changed_at": now_iso}
        if is_reachable != was_reachable:
            watch_state[name] = new_entry
            state_changed = True
        elif name not in watch_state:
            watch_state[name] = new_entry
            state_changed = True

    if state_changed:
        save_watch_state(watch_state)


# ── CLI ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Brother Agent — Offline multi-agent data mirroring",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Command")

    # add
    p = sub.add_parser("add", help="Register a new brother node")
    p.add_argument("name", help="Brother name (alphanumeric, dashes, underscores)")
    p.add_argument("--host", required=True, help="SSH hostname or IP")
    p.add_argument("--user", default="", help="SSH username")
    p.add_argument("--desc", default="", help="Description")

    # sync
    p = sub.add_parser("sync", help="Sync data with a brother")
    p.add_argument("name", nargs="?", default=None, help="Brother name")
    p.add_argument("--all", action="store_true", help="Sync all registered brothers")
    p.add_argument("--quiet", action="store_true", help="Suppress output for unreachable nodes")

    # status
    p = sub.add_parser("status", help="Show status")
    p.add_argument("name", nargs="?", default=None, help="Brother name (optional)")

    # list
    sub.add_parser("list", help="List all registered brothers")

    # remove
    p = sub.add_parser("remove", help="Remove a brother registration")
    p.add_argument("name", help="Brother name to remove")

    # watch
    sub.add_parser("watch", help="Edge-triggered auto-sync (for cron)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "add": cmd_add,
        "sync": cmd_sync,
        "status": cmd_status,
        "list": cmd_list,
        "remove": cmd_remove,
        "watch": cmd_watch,
    }

    try:
        commands[args.command](args)
    except Exception as e:
        log(f"{args.command} failed: {e}", "ERROR")
        sys.exit(1)


if __name__ == "__main__":
    main()
