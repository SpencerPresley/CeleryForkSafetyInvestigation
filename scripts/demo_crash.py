"""Demonstrates the fork-safety bug with ChromaDB/SQLite in Celery prefork pool.

This script simulates what happens in Celery when you initialize ChromaDB at
module level. The fork-unsafe pattern causes SIGTRAP when the child process
tries to use inherited database connections with corrupted mutex state.

Expected behavior: Child process crashes with SIGTRAP (signal 5).

Usage:
    python demo_crash.py                    # Normal run (auto-detects Docker)
    python demo_crash.py --docker           # Force Docker paths (/app/...)
    DEBUG_DELAY=1 python demo_crash.py      # Add 2s delay before crash
"""

from __future__ import annotations

import argparse
import contextlib
import faulthandler
import multiprocessing
import os
import resource
import signal
import subprocess
import sys
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_openai import OpenAIEmbeddings

if TYPE_CHECKING:
    from types import FrameType

load_dotenv()


# Signal codes
class SignalCode(IntEnum):
    """Unix signal codes for process termination."""

    SIGTRAP = 5  # MacOS when libdispatch detects semaphore state
    SIGABRT = 6
    SIGKILL = 9


# Script configuration - will be set by parse_args()
MAX_STACK_FRAMES = 5
MAX_LOCAL_VARS = 5
MAX_FD_DISPLAY = 3
MAX_VALUE_REPR_LEN = 100
DEBUG_DELAY_SECONDS = 2
CORE_DUMP_TIMEOUT = 5


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


# Global paths - will be initialized by parse_args()
CRASH_DUMP_DIR: Path
WORKER_SIGTRAP_FILE: Path
WORKER_PID_FILE: Path


@dataclass
class WorkerConfig:
    """Configuration for the worker task."""

    embedding_model: str
    api_key: str
    provider: str
    persist_directory: Path
    collection_name: str = "example_collection_broken"

    @classmethod
    def from_env(cls, base_path: Path) -> WorkerConfig:
        """Create configuration from environment variables.

        Returns:
            WorkerConfig instance populated from environment.

        Raises:
            ValueError: If required environment variables are missing.
        """
        provider = os.getenv("PROVIDER")
        api_key = os.getenv("API_KEY")
        embedding_model = os.getenv("EMBEDDING_MODEL")

        if not provider:
            raise ValueError("PROVIDER environment variable is required")
        if provider not in ("openai", "google"):
            raise ValueError(f"PROVIDER must be 'openai' or 'google', got: {provider}")
        if not api_key:
            raise ValueError("API_KEY environment variable is required")
        if not embedding_model:
            raise ValueError("EMBEDDING_MODEL environment variable is required")

        return cls(
            embedding_model=embedding_model,
            api_key=api_key,
            provider=provider,
            persist_directory=base_path / "chroma_db_demo_crash",
        )


# Module-level globals - initialized by init_globals()
config: WorkerConfig
embedding_broken: OpenAIEmbeddings | GoogleGenerativeAIEmbeddings
vector_store_broken: Chroma


def init_globals(use_docker: bool | None = None) -> None:
    """Initialize global configuration and paths.

    Args:
        use_docker: Force Docker paths (True) or local paths (False).
                   None = auto-detect.

    Side effects:
        - Sets global CRASH_DUMP_DIR, WORKER_SIGTRAP_FILE, WORKER_PID_FILE
        - Sets global config, embedding_broken, vector_store_broken
        - Creates crash dump directory
    """
    global CRASH_DUMP_DIR, WORKER_SIGTRAP_FILE, WORKER_PID_FILE
    global config, embedding_broken, vector_store_broken

    # Set up paths
    base_path = get_base_path(use_docker)
    CRASH_DUMP_DIR = base_path / "crash_dumps"
    WORKER_SIGTRAP_FILE = CRASH_DUMP_DIR / "worker_sigtrap.txt"
    WORKER_PID_FILE = CRASH_DUMP_DIR / "worker_pid.txt"

    # Initialize configuration
    config = WorkerConfig.from_env(base_path)

    # Create output directory for crash dumps before forking
    CRASH_DUMP_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[INIT] Initializing globals in PID {os.getpid()}")
    print(
        f"[CONFIG] Using {
            'Docker'
            if (use_docker if use_docker is not None else is_running_in_docker())
            else 'local'
        } paths: {base_path}"
    )

    # Fork-unsafe pattern: Initialize ChromaDB in parent process (before fork)
    # This simulates module-level initialization that happens in Celery workers
    if config.provider == "openai":
        embedding_broken = OpenAIEmbeddings(
            model=config.embedding_model,
            api_key=config.api_key,
        )
    elif config.provider == "google":
        embedding_broken = GoogleGenerativeAIEmbeddings(
            model=config.embedding_model,
            google_api_key=config.api_key,
        )

    vector_store_broken = Chroma(
        collection_name=config.collection_name,
        embedding_function=embedding_broken,
        persist_directory=str(config.persist_directory),
    )


@contextlib.contextmanager
def crash_dump_handler():
    """Context manager for registering crash dump handlers.

    Yields:
        None

    Side effects:
        - Registers faulthandler for SIGTRAP
        - Enables core dumps if possible
        - Writes worker PID to file
    """
    # Register faulthandler to dump child's stack when SIGTRAP fires
    fault_file = WORKER_SIGTRAP_FILE.open("w", buffering=1)
    faulthandler.register(signal.SIGTRAP, file=fault_file, all_threads=True, chain=True)

    # Enable core dumps to capture C/Rust level state
    try:
        resource.setrlimit(
            resource.RLIMIT_CORE, (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
        )
        print("[WORKER] Core dumps enabled")
    except Exception:
        pass

    # Write PID to file so parent can attach debugger if needed
    with WORKER_PID_FILE.open("w") as f:
        f.write(str(os.getpid()))

    try:
        yield
    finally:
        fault_file.close()


def print_worker_diagnostics() -> None:
    """Print diagnostic information about the worker process state."""
    print(f"[WORKER] Task executing in PID {os.getpid()}")
    print(f"[WORKER] Parent PID is {os.getppid()}")
    print(f"[WORKER] SIGTRAP handler registered - will dump to {WORKER_SIGTRAP_FILE}")

    # Introspect the inherited ChromaDB connection before crash
    print("\n[WORKER] Pre-crash introspection:")
    print(f"  vector_store type: {type(vector_store_broken)}")
    print(f"  vector_store id: {id(vector_store_broken)}")

    # Try to get SQLite connection info
    try:
        client = vector_store_broken._client
        print(f"  Client type: {type(client)}")
        if hasattr(client, "_conn"):
            print("  Has _conn attribute: True")
            print(f"  Connection object: {client._conn}")
        else:
            print("  Has _conn attribute: False")
    except Exception as e:
        print(f"  Could not inspect client: {e}")

    # Show open file descriptors
    print_file_descriptors()

    # Dump all local variables before crash
    print_local_variables()

    # Memory info
    print_memory_usage()


def print_file_descriptors() -> None:
    """Print open file descriptors for the current process."""
    print("\n[WORKER] Open file descriptors:")
    try:
        result = subprocess.run(
            ["lsof", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=1,
        )
        lines = result.stdout.split("\n")
        sqlite_fds = [line for line in lines if ".db" in line or "chroma" in line]
        print(f"  Total FDs: {len(lines)}")
        print(f"  SQLite/Chroma FDs: {len(sqlite_fds)}")
        for fd in sqlite_fds[:MAX_FD_DISPLAY]:
            print(f"    {fd}")
    except Exception as e:
        print(f"  Could not list FDs: {e}")


def print_local_variables() -> None:
    """Print local variables in the current frame."""
    print("\n[WORKER] Local variables in scope:")
    frame = sys._getframe(1)  # Get caller's frame
    for name, value in frame.f_locals.items():
        value_repr = repr(value)[:MAX_VALUE_REPR_LEN]
        print(f"  {name} = {value_repr}")


def print_memory_usage() -> None:
    """Print memory usage statistics."""
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        print("\n[WORKER] Memory usage:")
        print(f"  Max RSS: {usage.ru_maxrss / 1024 / 1024:.2f} MB")
    except Exception:
        pass


def signal_embeddings_complete() -> None:
    """Signal parent process that embeddings are done, ChromaDB operations next.

    Raises:
        Exception: If embedding or signaling fails (non-fatal, continues anyway).
    """
    try:
        parent_pid = os.getppid()
        os.kill(parent_pid, signal.SIGUSR1)
        print(
            "[WORKER] Sent SIGUSR1 to parent - embeddings done, "
            "ChromaDB operations next"
        )
    except Exception as e:
        print(f"[WORKER] Could not signal parent: {e}")


def worker_task() -> str:
    """Worker task that demonstrates the fork-safety bug.

    This uses the fork-unsafe pattern (module-level ChromaDB initialization)
    and should crash with SIGTRAP when trying to access the database.

    Returns:
        Success message if documents are inserted (unexpected).

    Raises:
        SystemExit: If the process crashes with SIGTRAP (expected).
    """
    with crash_dump_handler():
        # Prepare test documents
        docs = [
            {"page_content": "Test document 1", "metadata": {"source": "test"}},
            {"page_content": "Test document 2", "metadata": {"source": "test"}},
        ]
        documents = [Document(**doc) for doc in docs]
        uuids = [str(uuid4()) for _ in range(len(documents))]

        # Print diagnostic information
        print_worker_diagnostics()

        print(
            "\n[WORKER] Attempting to add documents to inherited ChromaDB connection..."
        )

        # Get embeddings first (this makes the OpenAI API call)
        try:
            _ = embedding_broken.embed_documents(
                [doc.page_content for doc in documents]
            )
            signal_embeddings_complete()
        except Exception as e:
            # If embedding fails, still try ChromaDB (might fail differently)
            print(f"[WORKER] Embedding failed: {e}, proceeding anyway...")

        # Optional delay for debugging - set DEBUG_DELAY=1 to enable
        if os.getenv("DEBUG_DELAY") == "1":
            print(
                f"[WORKER] DEBUG_DELAY enabled - waiting {DEBUG_DELAY_SECONDS} "
                "seconds before crash..."
            )
            import time

            time.sleep(DEBUG_DELAY_SECONDS)

        # This is where the SIGTRAP/deadlock should happen
        # ChromaDB operations will try to acquire corrupted mutexes
        vector_store_broken.add_documents(documents=documents, ids=uuids)

        print("[WORKER] document ingestion is done. (This means it didn't crash)")
        return f"Inserted {len(documents)} documents"


def sigchld_handler(signum: int, frame: FrameType | None) -> None:
    """Parent receives SIGCHLD when child process exits.

    Args:
        signum: Signal number received.
        frame: Current stack frame (may be None).
    """
    os.write(2, b"\n[PARENT] SIGCHLD received (signal ")
    os.write(2, str(signum).encode())
    os.write(2, b") - child exited\n")

    if not frame:
        return

    # Dump all frame info
    os.write(2, b"[PARENT] Frame details:\n")
    os.write(2, b"  Function: ")
    os.write(2, frame.f_code.co_name.encode())
    os.write(2, b"\n  File: ")
    os.write(2, frame.f_code.co_filename.encode())
    os.write(2, b"\n  Line: ")
    os.write(2, str(frame.f_lineno).encode())
    os.write(2, b"\n  First line of function: ")
    os.write(2, str(frame.f_code.co_firstlineno).encode())
    os.write(2, b"\n  Last instruction: ")
    os.write(2, str(frame.f_lasti).encode())
    os.write(2, b"\n  Local vars: ")
    os.write(2, str(list(frame.f_locals.keys())).encode())
    os.write(2, b"\n  Global vars count: ")
    os.write(2, str(len(frame.f_globals)).encode())
    os.write(2, b"\n")

    # Walk the call stack (f_back chain)
    os.write(2, b"\n[PARENT] Call stack:\n")
    current: FrameType | None = frame
    depth = 0
    while current and depth < MAX_STACK_FRAMES:
        os.write(2, b"  ")
        os.write(2, str(depth).encode())
        os.write(2, b": ")
        os.write(2, current.f_code.co_name.encode())
        os.write(2, b"() at line ")
        os.write(2, str(current.f_lineno).encode())
        os.write(2, b"\n")
        current = current.f_back
        depth += 1

    # Dump some local variable values (be selective)
    os.write(2, b"\n[PARENT] Local variables:\n")
    for key in list(frame.f_locals.keys())[:MAX_LOCAL_VARS]:
        os.write(2, b"  ")
        os.write(2, key.encode())
        os.write(2, b" = ")
        os.write(2, str(frame.f_locals[key])[:50].encode())
        os.write(2, b"\n")

def forward_sigusr1(signum: int, frame: FrameType | None) -> None:
    """Forward SIGUSR1 from worker to our parent (gdb_attach_child.py).

    Args:
        signum: Signal number received.
        frame: Current stack frame.
    """
    del signum, frame  # Unused but required by signal handler signature
    parent_pid = os.getppid()
    try:
        os.kill(parent_pid, signal.SIGUSR1)
    except Exception as e:
        # Only matters when running under gdb_attach_child.py / lldb_attach_child.py
        # Safe to ignore for standalone usage
        print(f"[DEBUG] Could not forward SIGUSR1 to parent: {e}")


def display_crash_stack_trace() -> None:
    """Display the worker's crash stack trace if available."""
    if WORKER_SIGTRAP_FILE.exists():
        print("\n[WORKER CRASH STACK TRACE]")
        print("=" * 60)
        with WORKER_SIGTRAP_FILE.open() as f:
            print(f.read())
        print("=" * 60)


def extract_core_backtrace() -> None:
    """Extract and display C/Rust backtrace from core dump if available."""
    core_files = list(Path.cwd().glob("core.*")) + list(Path("/cores").glob("core.*"))

    if core_files:
        core_file = core_files[0]
        print(f"\n[INFO] Core dump generated: {core_file}")
        print("[INFO] Extracting C/Rust backtrace...")

        try:
            result = subprocess.run(
                ["lldb", "-c", str(core_file), "--batch", "-o", "bt"],
                capture_output=True,
                text=True,
                timeout=CORE_DUMP_TIMEOUT,
            )
            print("\n[C/RUST BACKTRACE FROM CORE]")
            print("=" * 60)
            print(result.stdout)
            print("=" * 60)
        except Exception as e:
            print(f"[INFO] Could not extract backtrace: {e}")
            print(f"[INFO] Manual inspect: lldb -c {core_file}")
    else:
        print("\n[INFO] No core dump generated (macOS often disables them)")
        print("[INFO] The SIGTRAP came from SQLite's internal assertion checks")
        print(
            "[INFO] This fires when SQLite detects corrupted state "
            "(e.g. bad pointers after fork)"
        )


def handle_signal_exit(signum: int) -> None:
    """Handle worker process killed by signal.

    Args:
        signum: Signal number that killed the worker.
    """
    print(f"\n[RESULT] Worker killed by signal {signum}")

    if signum == SignalCode.SIGTRAP:
        print(
            "[RESULT] SIGTRAP confirmed - this is the fork-safety bug "
            "with ChromaDB/SQLite"
        )
        display_crash_stack_trace()
        extract_core_backtrace()
    else:
        print("[RESULT] Unexpected signal")


def run_fork_safety_demo() -> None:
    """Run the fork-safety demonstration."""
    # Use fork explicitly (simulates Celery's prefork pool)
    multiprocessing.set_start_method("fork", force=True)

    # Set up signal handlers
    signal.signal(signal.SIGUSR1, forward_sigusr1)
    signal.signal(signal.SIGCHLD, sigchld_handler)

    # Fork and run worker task
    worker = multiprocessing.Process(target=worker_task)
    worker.start()

    # Wait for child to exit with timeout - child might deadlock on Linux
    worker.join(timeout=10)  # 10 second timeout

    if worker.is_alive():
        print("\n[RESULT] Worker deadlocked (timeout after 10s)")
        print("[RESULT] This is the fork-safety bug - child is stuck in futex wait")
        print("[RESULT] Killing worker process...")
        worker.terminate()
        worker.join(timeout=2)
        if worker.is_alive():
            worker.kill()
            worker.join()
        exit_code = worker.exitcode
    else:
        exit_code = worker.exitcode

    # Decode the exit code to determine what happened
    if exit_code is None:
        print("\n[RESULT] Worker process still running (unexpected)")
    elif exit_code < 0:
        handle_signal_exit(abs(exit_code))
    elif exit_code == 0:
        print("\n[RESULT] Worker succeeded (didn't crash)")
    else:
        print(f"\n[RESULT] Worker exited with code {exit_code}")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Demonstrate fork-safety bug with ChromaDB/SQLite in Celery prefork pool"
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
    init_globals(args.docker)
    print("\n=== Simulating Celery's Prefork Behavior ===\n")
    run_fork_safety_demo()
