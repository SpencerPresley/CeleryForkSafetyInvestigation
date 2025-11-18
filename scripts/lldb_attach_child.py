#!/usr/bin/env python3
"""Attach LLDB to child process for debugging ChromaDB fork-safety issues (macOS).

This script launches demo_crash.py and attaches LLDB to the child process to
catch the SIGTRAP when it occurs. Uses DEBUG_DELAY=1 to give time for LLDB to
attach before the crash happens.

Platform: macOS only (uses LLDB)

Usage:
    python lldb_attach_child.py                 # Auto-detect Docker
    python lldb_attach_child.py --docker        # Force Docker paths (/app/...)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil


def is_running_in_docker() -> bool:
    """Detect if we're running inside a Docker container.

    Returns:
        True if running in Docker, False otherwise.
    """
    return Path("/.dockerenv").exists() or os.getenv("DOCKER_CONTAINER") == "1"


def get_base_path(use_docker: bool | None = None) -> Path:
    """Get base path for output directories.

    Args:
        use_docker: Force Docker paths (True) or local paths (False).
                   None = auto-detect.

    Returns:
        Base path (/app for Docker, current directory for local).
    """
    if use_docker is None:
        use_docker = is_running_in_docker()

    return Path("/app") if use_docker else Path.cwd()


# Constants - will be set by main()
CRASH_DUMP_DIR: Path
WORKER_PID_FILE: Path
LLDB_SCRIPT_PATH = Path("/tmp/lldb_attach_child.txt")

# Timing constants
PROCESS_START_DELAY = 0.1  # seconds
CHILD_FIND_TIMEOUT = 2.0  # seconds
CHILD_FIND_INTERVAL = 0.005  # seconds
LLDB_TIMEOUT = 10  # seconds

# LLDB script template
# This script is designed to catch SIGTRAP in forked processes
LLDB_SCRIPT_TEMPLATE = """# Attach to the child process
process attach --pid {child_pid}

# Catch SIGTRAP
process handle SIGTRAP --stop true --notify true --pass false

# Set breakpoints on signal functions
breakpoint set -n raise
breakpoint set -n kill
breakpoint set -n pthread_kill
breakpoint set -n abort

# Continue and wait for SIGTRAP
continue

# When SIGTRAP hits, show backtrace
bt
frame info
disassemble --frame
register read

# Quit
quit
"""


def clear_stale_pid_file() -> None:
    """Remove stale PID file from previous runs."""
    if WORKER_PID_FILE.exists():
        WORKER_PID_FILE.unlink()
        print("Cleared stale PID file")


def start_demo_process(use_docker: bool | None = None) -> subprocess.Popen:
    """Start the demo_crash.py process with DEBUG_DELAY enabled.

    Args:
        use_docker: Pass to demo_crash.py (True/False/None for auto-detect).

    Returns:
        Popen object for the parent process.
    """
    base_path = get_base_path(use_docker)
    cmd = [sys.executable, "demo_crash.py"]

    # Pass Docker flag to demo_crash.py
    if use_docker is True:
        cmd.append("--docker")

    proc = subprocess.Popen(
        cmd,
        cwd=str(base_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "DEBUG_DELAY": "1"},  # Enable debug delay for LLDB
    )
    print(f"Started parent PID: {proc.pid}")
    print(f"Using Python interpreter: {sys.executable}")
    print(f"Working directory: {base_path}")
    print("Waiting for child process...")
    time.sleep(PROCESS_START_DELAY)
    return proc


def find_child_via_psutil(parent_pid: int) -> int | None:
    """Find child process PID using psutil.

    Args:
        parent_pid: Parent process PID.

    Returns:
        Child PID if found, None otherwise.
    """
    try:
        parent = psutil.Process(parent_pid)
        children = parent.children(recursive=True)
        if children:
            child_pid = children[0].pid
            child_proc = psutil.Process(child_pid)
            if child_proc.ppid() == parent_pid:
                print(f"Found child PID via psutil: {child_pid}")
                return child_pid
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return None


def find_child_via_pid_file(parent_pid: int) -> int | None:
    """Find child process PID using PID file.

    Args:
        parent_pid: Parent process PID.

    Returns:
        Child PID if found, None otherwise.
    """
    if not WORKER_PID_FILE.exists():
        return None

    try:
        with WORKER_PID_FILE.open() as f:
            file_pid = int(f.read().strip())
        # Verify it's actually a child of our parent
        file_proc = psutil.Process(file_pid)
        if file_proc.ppid() == parent_pid:
            print(f"Found child PID via file: {file_pid}")
            return file_pid
    except (ValueError, psutil.NoSuchProcess, OSError):
        pass
    return None


def find_child_process(parent_pid: int) -> int | None:
    """Find the child process PID using multiple strategies.

    Args:
        parent_pid: Parent process PID.

    Returns:
        Child PID if found, None otherwise.
    """
    max_attempts = int(CHILD_FIND_TIMEOUT / CHILD_FIND_INTERVAL)

    for attempt in range(max_attempts):
        time.sleep(CHILD_FIND_INTERVAL)

        # Try psutil first
        child_pid = find_child_via_psutil(parent_pid)
        if child_pid:
            return child_pid

        # Fall back to PID file
        child_pid = find_child_via_pid_file(parent_pid)
        if child_pid:
            return child_pid

        # Only print errors on first attempt
        if attempt == 0:
            try:
                psutil.Process(parent_pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            except Exception as e:
                print(f"Error checking for child: {e}")

    return None


def print_process_debug_info(proc: subprocess.Popen) -> None:
    """Print debug information when child process is not found.

    Args:
        proc: Parent process Popen object.
    """
    try:
        parent_proc = psutil.Process(proc.pid)
        print(f"Parent PID {proc.pid} status: {parent_proc.status()}")
        children = parent_proc.children(recursive=True)
        print(f"Children found: {[c.pid for c in children]}")

        if WORKER_PID_FILE.exists():
            with WORKER_PID_FILE.open() as f:
                print(f"PID file contains: {f.read().strip()}")
    except Exception as e:
        print(f"Error getting debug info: {e}")


def create_lldb_script(child_pid: int) -> str:
    """Create LLDB command script for debugging.

    Args:
        child_pid: Child process PID to attach to.

    Returns:
        LLDB script content as string.
    """
    return LLDB_SCRIPT_TEMPLATE.format(child_pid=child_pid)


def run_lldb_attach(child_pid: int) -> None:
    """Attach LLDB to child process and capture output.

    Args:
        child_pid: Child process PID to attach to.
    """
    print(f"\nAttaching lldb to child PID {child_pid}...")
    print("This will catch SIGTRAP when it happens.\n")

    # Create and write LLDB script
    lldb_script = create_lldb_script(child_pid)
    with LLDB_SCRIPT_PATH.open("w") as f:
        f.write(lldb_script)

    # Run lldb
    try:
        result = subprocess.run(
            ["lldb", "--batch", "-s", str(LLDB_SCRIPT_PATH)],
            capture_output=True,
            text=True,
            timeout=LLDB_TIMEOUT,
        )

        print("=== LLDB OUTPUT ===")
        print(result.stdout)
        if result.stderr:
            print("\n=== LLDB STDERR ===")
            print(result.stderr)

    except subprocess.TimeoutExpired:
        print("\n[INFO] LLDB timed out - process may have completed before attach")
    except Exception as e:
        print(f"[ERROR] Failed to run LLDB: {e}")


def attach_to_child(use_docker: bool | None = None) -> None:
    """Main function to attach LLDB to child process.

    Args:
        use_docker: Force Docker paths (True) or local paths (False).
                   None = auto-detect.
    """
    global CRASH_DUMP_DIR, WORKER_PID_FILE

    # Initialize paths
    base_path = get_base_path(use_docker)
    CRASH_DUMP_DIR = base_path / "crash_dumps"
    WORKER_PID_FILE = CRASH_DUMP_DIR / "worker_pid.txt"

    print("=" * 80)
    print("ATTACHING LLDB TO CHILD PROCESS (macOS)")
    print("=" * 80)
    print(
        f"Using {
            'Docker'
            if (use_docker if use_docker is not None else is_running_in_docker())
            else 'local'
        } paths: {base_path}\n"
    )

    # Setup and start process
    clear_stale_pid_file()
    proc = start_demo_process(use_docker)

    # Find child process
    child_pid = find_child_process(proc.pid)
    if not child_pid:
        print("Could not find child PID, exiting")
        print_process_debug_info(proc)
        return

    # Attach LLDB and debug
    run_lldb_attach(child_pid)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Attach LLDB to child process for debugging ChromaDB fork-safety issues"
        )
    )
    parser.add_argument(
        "--docker",
        action="store_true",
        default=None,
        help="Use Docker paths (/app/...). Default: auto-detect",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    attach_to_child(args.docker)
