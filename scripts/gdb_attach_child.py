#!/usr/bin/env python3
"""Attach GDB to child process for debugging ChromaDB fork-safety issues (Linux).

This script launches demo_crash.py and attaches GDB to the child process to
inspect the deadlock or crash. It waits for SIGUSR1 signal from the child to
know when embeddings are done and ChromaDB operations are about to start.

Platform: Linux only (uses GDB and futex-specific debugging)

Usage:
    python gdb_attach_child.py                  # Auto-detect Docker
    python gdb_attach_child.py --docker         # Force Docker paths (/app/...)
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from types import FrameType


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
GDB_SCRIPT_PATH = Path("/tmp/gdb_attach_child.txt")

# Timing constants
PROCESS_START_DELAY = 0.5  # seconds
CHILD_FIND_TIMEOUT = 2.0  # seconds
CHILD_FIND_INTERVAL = 0.005  # seconds
SIGNAL_WAIT_TIMEOUT = 30  # seconds
SIGNAL_WAIT_INTERVAL = 0.1  # seconds
DEADLOCK_PAUSE = 0.5  # seconds
FALLBACK_WAIT = 1.0  # seconds
GDB_TIMEOUT = 30  # seconds
GDB_QUICK_TIMEOUT = 10  # seconds
COMM_TIMEOUT = 1  # seconds
OUTPUT_TAIL_CHARS = 500

# GDB register constants (ARM64 Linux)
FUTEX_SYSCALL_NUMBER = 0xCA  # 202 in decimal

# GDB script template
# This script is designed to catch futex deadlocks or SIGTRAP in forked processes
# SIGTRAP behavior for linux has not been observed, so most likely results in a deadlock
GDB_SCRIPT_TEMPLATE = """# Attach to the child process
attach {child_pid}

# Process might be:
# 1. In network I/O (OpenAI API call) - wait for it to finish
# 2. Already deadlocked on futex - inspect immediately
# 3. About to deadlock - continue briefly

# Show current state first
info threads
thread apply all bt

# If process is in network I/O (BIO_read, SSL operations), continue to let it finish
# Then it will hit the ChromaDB deadlock
# Use a short timeout - if it hangs, it's likely deadlocked
set confirm off
continue

# If we get here, process stopped (unlikely - will hang on deadlock)
# Show final state
info threads
thread apply all bt

# Show registers to identify futex addresses
thread 1
info registers
disassemble $pc

# On ARM64 Linux: x8 = syscall number (0x{futex_syscall:x} for futex)
# For futex: x1 = futex address
print $x8
print $x1
print $x2

# Show memory at futex address (if it's a futex wait)
# x1 should contain the futex address
x/1gx $x1

# Quit
quit
"""


# Global state for signal handler
class SignalState:
    """Container for signal handler state."""

    embeddings_done: bool = False


signal_state = SignalState()


def sigusr1_handler(signum: int, frame: FrameType | None) -> None:
    """Handle SIGUSR1 from child process - embeddings are done.

    Args:
        signum: Signal number received.
        frame: Current stack frame.

    Note:
        This handler is async-signal-safe - it only sets a flag.
        No printing or complex operations here.
    """
    del signum, frame  # Unused but required by signal handler signature
    signal_state.embeddings_done = True


def clear_stale_pid_file() -> None:
    """Remove stale PID file from previous runs."""
    if WORKER_PID_FILE.exists():
        WORKER_PID_FILE.unlink()
        print("Cleared stale PID file")


def start_demo_process(use_docker: bool | None = None) -> subprocess.Popen:
    """Start the demo_crash.py process using the current Python interpreter.

    Args:
        use_docker: Pass to demo_crash.py (True/False/None for auto-detect).

    Returns:
        Popen object for the parent process.
    """
    base_path = get_base_path(use_docker)
    cmd = [sys.executable, "scripts/demo_crash.py"]

    # Pass Docker flag to demo_crash.py
    if use_docker is True:
        cmd.append("--docker")
    elif use_docker is False:
        cmd.append("--no-docker")

    proc = subprocess.Popen(
        cmd,
        cwd=str(base_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ},  # No DEBUG_DELAY - want immediate deadlock
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
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                print(f"Error accessing process: {e}")
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

        # Check if parent has exited
        if not parent_proc.is_running():
            print(f"Parent process {proc.pid} has exited")
            # Try to get stdout/stderr
            try:
                stdout, stderr = proc.communicate(timeout=COMM_TIMEOUT)
                if stdout:
                    print("\n=== PARENT STDOUT ===")
                    decoded = stdout.decode("utf-8", errors="replace")
                    print(decoded[-OUTPUT_TAIL_CHARS:])
                if stderr:
                    print("\n=== PARENT STDERR ===")
                    decoded = stderr.decode("utf-8", errors="replace")
                    print(decoded[-OUTPUT_TAIL_CHARS:])
            except Exception:
                pass

        if WORKER_PID_FILE.exists():
            with WORKER_PID_FILE.open() as f:
                print(f"PID file contains: {f.read().strip()}")
    except Exception as e:
        print(f"Error getting debug info: {e}")


def wait_for_embeddings_signal(child_pid: int) -> bool:
    """Wait for SIGUSR1 signal indicating embeddings are done.

    Args:
        child_pid: Child process PID.

    Returns:
        True if signal received, False if timed out.
    """
    print("\nWaiting for SIGUSR1 signal from child...")
    print("(Child will signal when embeddings are done, ChromaDB deadlock is next)\n")

    signal_state.embeddings_done = False
    max_attempts = int(SIGNAL_WAIT_TIMEOUT / SIGNAL_WAIT_INTERVAL)

    for _ in range(max_attempts):
        time.sleep(SIGNAL_WAIT_INTERVAL)

        if signal_state.embeddings_done:
            print("  Received SIGUSR1 - embeddings done")
            time.sleep(DEADLOCK_PAUSE)  # Brief pause for deadlock to occur
            return True

        # Check if child is still running
        try:
            child_proc = psutil.Process(child_pid)
            if not child_proc.is_running():
                print("Child process exited before signaling")
                return False
        except psutil.NoSuchProcess:
            print("Child process exited")
            return False

    return False


def create_gdb_script(child_pid: int) -> str:
    """Create GDB command script for debugging.

    Args:
        child_pid: Child process PID to attach to.

    Returns:
        GDB script content as string.
    """
    return GDB_SCRIPT_TEMPLATE.format(
        child_pid=child_pid, futex_syscall=FUTEX_SYSCALL_NUMBER
    )


def run_gdb_attach(child_pid: int) -> None:
    """Attach GDB to child process and capture output.

    Args:
        child_pid: Child process PID to attach to.
    """
    print(f"\nAttaching gdb to child PID {child_pid}...")
    print("(Should catch ChromaDB deadlock now)\n")

    # Create and write GDB script
    gdb_script = create_gdb_script(child_pid)
    with GDB_SCRIPT_PATH.open("w") as f:
        f.write(gdb_script)

    # Run gdb (non-interactive mode)
    try:
        result = subprocess.run(
            ["gdb", "--batch", "-x", str(GDB_SCRIPT_PATH)],
            capture_output=True,
            text=True,
            timeout=GDB_TIMEOUT,
        )
        print("=== GDB OUTPUT ===")
        print(result.stdout)
        if result.stderr:
            print("\n=== GDB STDERR ===")
            print(result.stderr)

    except subprocess.TimeoutExpired:
        print("\n[INFO] GDB timed out - process likely hung (deadlock)")
        print("This is expected Linux behavior - mutex deadlock instead of crash")
        run_quick_backtrace(child_pid)

    # Check if child is still running (might be hung)
    check_child_status(child_pid)


def run_quick_backtrace(child_pid: int) -> None:
    """Run quick GDB backtrace when main attach times out.

    Args:
        child_pid: Child process PID.
    """
    try:
        quick_bt = subprocess.run(
            [
                "gdb",
                "--batch",
                "-ex",
                f"attach {child_pid}",
                "-ex",
                "info threads",
                "-ex",
                "thread apply all bt",
                "-ex",
                "thread apply all info registers",
                "-ex",
                "thread apply all print $x0",
                "-ex",
                "thread apply all print $x1",
                "-ex",
                "thread apply all print $x2",
                "-ex",
                "disassemble $pc",
                "-ex",
                "quit",
            ],
            capture_output=True,
            text=True,
            timeout=GDB_QUICK_TIMEOUT,
        )
        print("\n=== DETAILED BACKTRACE (process hung) ===")
        print(quick_bt.stdout)
        if quick_bt.stderr:
            print("\n=== GDB STDERR ===")
            print(quick_bt.stderr)
    except Exception as e:
        print(f"Error getting backtrace: {e}")


def check_child_status(child_pid: int) -> None:
    """Check and report child process status.

    Args:
        child_pid: Child process PID.
    """
    try:
        child_proc = psutil.Process(child_pid)
        if child_proc.is_running():
            print(
                f"\n[WARNING] Child process {child_pid} is still running (likely hung)"
            )
            print("This matches the Linux behavior - deadlock instead of crash")
    except psutil.NoSuchProcess:
        print(f"\nChild process {child_pid} has exited")


def attach_to_child(use_docker: bool | None = None) -> None:
    """Main function to attach GDB to child process.

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
    print("ATTACHING GDB TO CHILD PROCESS (Linux)")
    print("=" * 80)
    print(
        f"Using {
            'Docker'
            if (use_docker if use_docker is not None else is_running_in_docker())
            else 'local'
        } paths: {base_path}\n"
    )

    # Install SIGUSR1 handler to detect when embeddings are done
    signal.signal(signal.SIGUSR1, sigusr1_handler)

    # Setup and start process
    clear_stale_pid_file()
    proc = start_demo_process(use_docker)

    # Find child process
    child_pid = find_child_process(proc.pid)
    if not child_pid:
        print("Could not find child PID, exiting")
        print_process_debug_info(proc)
        return

    # Wait for signal from child
    signal_received = wait_for_embeddings_signal(child_pid)
    if not signal_received:
        print("  No SIGUSR1 received - child may have already passed embeddings")
        print("  Attaching anyway...")
        time.sleep(FALLBACK_WAIT)

    # Attach GDB and debug
    run_gdb_attach(child_pid)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Attach GDB to child process for debugging ChromaDB fork-safety issues"
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
