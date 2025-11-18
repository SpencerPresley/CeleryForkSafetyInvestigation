"""A/B test runner for comparing Celery worker pools.

This script tests whether gevent/eventlet/threads pools avoid the fork-safety issue
when using the fork-unsafe pattern (module-level ChromaDB initialization).

Prerequisites:
    pip install gevent eventlet celery redis langchain-chroma langchain-openai

Usage:
    python run_test.py --pool prefork   # Should fail (either deadlock or SIGTRAP)
    python run_test.py --pool gevent    # Should work
    python run_test.py --pool eventlet  # Should work
    python run_test.py --pool threads   # Should work
"""

import argparse
import contextlib
import importlib.util
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum, IntEnum
from pathlib import Path

import redis
import tasks_pool_test
from celery.exceptions import TimeoutError as CeleryTimeoutError
from celery.exceptions import WorkerLostError


class ValidationError(Exception):
    """Raised when command line argument validation fails."""


class DependencyError(Exception):
    """Raised when a required dependency is missing."""


class ExitCode(IntEnum):
    """Exit codes for the test runner.

    Following standard Unix exit code conventions:
    - 0: Success
    - 1-127: Various error conditions
    """

    SUCCESS = 0
    GENERAL_ERROR = 1
    VALIDATION_ERROR = 2
    DEPENDENCY_ERROR = 3
    TEST_FAILED = 4
    UNEXPECTED_TEST_RESULTS = 5


class PoolType(str, Enum):
    """Available Celery worker pool types."""

    PREFORK = "prefork"
    GEVENT = "gevent"
    EVENTLET = "eventlet"
    THREADS = "threads"

    @classmethod
    def choices(cls) -> list[str]:
        """Return list of valid pool type choices."""
        return [pool.value for pool in cls]

    @classmethod
    def from_string(cls, value: str) -> "PoolType":
        """Convert string to PoolType enum.

        Args:
            value: Pool type string.

        Returns:
            PoolType enum value.

        Raises:
            ValueError: If value is not a valid pool type.
        """
        try:
            return cls(value)
        except ValueError as e:
            valid = ", ".join(cls.choices())
            raise ValueError(
                f"Invalid pool type '{value}'. Must be one of: {valid}"
            ) from e


@dataclass(frozen=True)
class PoolConfig:
    """Configuration for a pool type."""

    pool_type: PoolType
    dependencies: list[str]
    expected_to_pass: bool
    description: str

    def check_dependencies(self) -> list[str]:
        """Check which dependencies are missing.

        Returns:
            List of missing dependency names.
        """
        return [
            dep for dep in self.dependencies if importlib.util.find_spec(dep) is None
        ]


# Pool configurations - single source of truth
POOL_CONFIGS: dict[PoolType, PoolConfig] = {
    PoolType.PREFORK: PoolConfig(
        pool_type=PoolType.PREFORK,
        dependencies=[],
        expected_to_pass=False,
        description="Prefork pool (fork-based, expects fork-safety failure)",
    ),
    PoolType.GEVENT: PoolConfig(
        pool_type=PoolType.GEVENT,
        dependencies=["gevent"],
        expected_to_pass=True,
        description="Gevent pool (cooperative multitasking, no fork)",
    ),
    PoolType.EVENTLET: PoolConfig(
        pool_type=PoolType.EVENTLET,
        dependencies=["eventlet"],
        expected_to_pass=True,
        description="Eventlet pool (cooperative multitasking, no fork)",
    ),
    PoolType.THREADS: PoolConfig(
        pool_type=PoolType.THREADS,
        dependencies=[],
        expected_to_pass=True,
        description="Threads pool (thread-based, shared memory)",
    ),
}


# Constants
DEFAULT_CONCURRENCY = 2
MAX_CONCURRENCY = 1000
MIN_CONCURRENCY = 1
MIN_PORT = 1
MAX_PORT = 65535
DEFAULT_REDIS_PORTS = [6379, 6380]  # Try standard port first
WORKER_READY_TIMEOUT = 10
TASK_TIMEOUT = 30
WORKER_SHUTDOWN_TIMEOUT = 10
TEST_PAUSE_SECONDS = 3  # Increased to ensure cleanup completes


def get_pool_config(pool_type: PoolType | str) -> PoolConfig:
    """Get configuration for a pool type.

    Args:
        pool_type: Pool type to get configuration for.

    Returns:
        Pool configuration.

    Raises:
        ValueError: If pool type is invalid.
    """
    if isinstance(pool_type, str):
        pool_type = PoolType.from_string(pool_type)

    return POOL_CONFIGS[pool_type]


def check_pool_dependency(pool_type: PoolType | str) -> None:
    """Check if required dependencies are available for a pool type.

    Args:
        pool_type: Pool type to check.

    Raises:
        DependencyError: If required dependencies for the pool type are missing.
    """
    config = get_pool_config(pool_type)
    missing = config.check_dependencies()

    if missing:
        deps_str = ", ".join(missing)
        install_cmd = f"pip install {' '.join(missing)}"
        raise DependencyError(
            f"Pool type '{config.pool_type.value}' requires the "
            f"following dependencies: {deps_str}. "
            f"Install with: {install_cmd}"
        )


# Add parent directory to path to import celery app
sys.path.insert(0, str(Path(__file__).parent.parent))


def check_redis(port: int | None = None) -> int:
    """Check if Redis is running on common ports.

    Args:
        port: Specific port to check, or None to auto-detect.

    Returns:
        Port number if Redis is found.

    Raises:
        RuntimeError: If Redis is not accessible on any checked ports.
    """
    redis_host = os.getenv("REDIS_HOST", "localhost")

    # Determine ports to try: CLI arg > env var > defaults
    if port:
        ports_to_try = [port]
    elif redis_port_env := os.getenv("REDIS_PORT"):
        ports_to_try = [int(redis_port_env)]
    else:
        ports_to_try = DEFAULT_REDIS_PORTS

    for test_port in ports_to_try:
        try:
            r = redis.Redis(
                host=redis_host, port=test_port, db=0, socket_connect_timeout=1
            )
            r.ping()
            print(f"[INFO] Found Redis on {redis_host}:{test_port}")
            return test_port
        except Exception:
            continue

    if port:
        error_msg = (
            f"Redis not accessible on port {port}. "
            f"Ensure Redis is running on {redis_host}:{port}."
        )
    else:
        ports_str = ", ".join(map(str, DEFAULT_REDIS_PORTS))
        error_msg = (
            f"Redis not accessible on {redis_host} ports: {ports_str}. "
            f"Ensure Redis is running on one of these ports, "
            f"or use --redis-port to specify a different port."
        )

    raise RuntimeError(error_msg)


def start_worker(pool_type: str, concurrency: int = 2, broker_url: str | None = None):
    """Start a Celery worker with the specified pool type.

    Args:
        pool_type: Type of pool (prefork, gevent, eventlet, threads)
        concurrency: Number of concurrent workers
        broker_url: Optional broker URL to use (if None, uses env vars)

    Returns:
        subprocess.Popen: Process handle for the worker
    """
    # Put log files in crash_dumps (writable directory)
    # Use /app/crash_dumps in Docker, ./crash_dumps locally
    crash_dumps = (
        Path("/app/crash_dumps")
        if Path("/app/.env").exists()
        else Path.cwd() / "crash_dumps"
    )
    logfile_path = str(crash_dumps / f"worker_{pool_type}.log")

    worker_cmd = [
        "celery",
        "-A",
        "tasks_pool_test",
        "worker",
        "--pool",
        pool_type,
        "--concurrency",
        str(concurrency),
        "--loglevel",
        "debug",  # Use debug to see what's happening
        "--logfile",
        logfile_path,
    ]

    # Set broker URL via environment if provided
    env = os.environ.copy()
    if broker_url:
        env["CELERY_BROKER_URL"] = broker_url
        env["CELERY_RESULT_BACKEND"] = broker_url

    # Ensure Python can find the celery_examples module
    # The working directory is /app/celery_examples, so we need /app in PYTHONPATH
    # This ensures tasks_pool_test can be imported as a module
    pythonpath = env.get("PYTHONPATH", "")
    celery_examples_dir = str(Path(__file__).parent)
    parent_dir = str(Path(__file__).parent.parent)
    if pythonpath:
        env["PYTHONPATH"] = f"{celery_examples_dir}:{parent_dir}:{pythonpath}"
    else:
        env["PYTHONPATH"] = f"{celery_examples_dir}:{parent_dir}"

    print(f"[INFO] Starting worker with pool={pool_type}, concurrency={concurrency}")
    print(f"[INFO] Command: {' '.join(worker_cmd)}")
    print(f"[INFO] Working directory: {Path(__file__).parent}")
    print(f"[INFO] PYTHONPATH: {env.get('PYTHONPATH')}")
    if broker_url:
        print(f"[INFO] Using broker: {broker_url}")

    # Don't capture output so we can see what's happening
    return subprocess.Popen(
        worker_cmd,
        cwd=Path(__file__).parent,
        env=env,
    )


def cleanup_worker(worker_process: subprocess.Popen | None):
    """Clean up worker process gracefully, force kill if deadlocked.

    Args:
        worker_process: Process handle to clean up, or None if already cleaned up
    """
    if worker_process is None:
        return

    print("[INFO] Stopping worker...")
    # Try graceful shutdown first
    try:
        worker_process.terminate()
        worker_process.wait(timeout=WORKER_SHUTDOWN_TIMEOUT)
        print("[INFO] Worker stopped gracefully")
        return
    except subprocess.TimeoutExpired:
        print("[WARNING] Worker didn't stop gracefully, force killing...")

    # Force kill - worker might be deadlocked
    try:
        worker_process.kill()
        worker_process.wait(timeout=5)
        print("[INFO] Worker force killed - DEADLOCK")
    except subprocess.TimeoutExpired:
        print("[ERROR] Worker process still alive after kill, may be stuck")
    except ProcessLookupError:
        # Process already dead, that's fine
        print("[INFO] Worker process already terminated")
    except Exception as e:
        print(f"[WARNING] Error during worker cleanup: {e}")


@contextlib.contextmanager
def managed_worker(pool_type: str, concurrency: int = 2, broker_url: str | None = None):
    """Context manager for managing a Celery worker process.

    Args:
        pool_type: Type of pool (prefork, gevent, eventlet, threads)
        concurrency: Number of concurrent workers
        broker_url: Optional broker URL to use (if None, uses env vars)

    Yields:
        subprocess.Popen: Process handle for the worker
    """
    worker_process = None
    try:
        worker_process = start_worker(pool_type, concurrency, broker_url)
        yield worker_process
    finally:
        cleanup_worker(worker_process)


def wait_for_worker_ready(timeout: int = WORKER_READY_TIMEOUT) -> None:
    """Wait for worker to be ready.

    Args:
        timeout: Maximum seconds to wait for worker.
    """
    print(f"[INFO] Waiting for worker to be ready (timeout={timeout}s)...")
    time.sleep(10)  # Give worker more time to fully initialize
    print("[INFO] Worker should be ready now")


def run_test_task(pool_type=None):
    """Run a test task and return the result.

    Args:
        pool_type: The pool type being tested (for pool-specific handling)

    Returns:
        dict: Task result dictionary with status and details.

    Raises:
        RuntimeError: If the task fails, times out, or worker crashes.
    """
    docs = [
        {"page_content": "Test document 1", "metadata": {"source": "test"}},
        {"page_content": "Test document 2", "metadata": {"source": "test"}},
    ]

    print("[INFO] Sending task to worker...")

    # Different pools need different approaches
    if pool_type in ["prefork", "threads"]:
        # Use delay() for prefork and threads
        from tasks_pool_test import add_documents

        result = add_documents.delay(docs)
    else:
        # Use send_task for gevent and eventlet
        result = tasks_pool_test.celery_app.send_task(
            "tasks_pool_test.add_documents", args=[docs]
        )

    print(f"[INFO] Task ID: {result.id}")
    print(f"[INFO] Waiting for result (timeout={TASK_TIMEOUT}s)...")

    try:
        task_result = result.get(timeout=TASK_TIMEOUT)
        print(f"[INFO] Task completed: {task_result}")
        return task_result
    except CeleryTimeoutError as e:
        print(f"[ERROR] Task timed out after {TASK_TIMEOUT} seconds")
        print(f"[ERROR] Task state: {result.state}")
        raise RuntimeError(f"Task timed out: {e}") from e
    except WorkerLostError as e:
        print(f"[ERROR] Worker crashed or exited prematurely: {e}")
        print(f"[ERROR] Task state: {result.state}")
        raise RuntimeError(f"Worker crashed: {e}") from e
    except Exception as e:
        print(f"[ERROR] Task failed: {e}")
        print(f"[ERROR] Task state: {result.state}")
        print(f"[ERROR] Exception type: {type(e).__name__}")
        # Try to get more info from the result
        try:
            print(f"[ERROR] Task info: {result.info}")
        except Exception:
            pass
        raise RuntimeError(f"Task failed: {e}") from e


def cleanup_redis_queue(redis_url: str):
    """Clean up Redis queue and task results between tests.

    Args:
        redis_url: Redis connection URL
    """
    try:
        r = redis.from_url(redis_url)
        # Flush the database to clear any stuck tasks
        r.flushdb()
        print("[INFO] Redis queue cleaned")
    except Exception as e:
        print(f"[WARNING] Failed to clean Redis queue: {e}")


def test_pool(
    pool_type: PoolType | str,
    concurrency: int,
    redis_port: int,
) -> bool:
    """Test a specific pool type.

    Args:
        pool_type: Type of pool to test (PoolType enum or string).
        concurrency: Number of concurrent workers.
        redis_port: Redis port to use.

    Returns:
        True if test passed, False otherwise.

    Raises:
        RuntimeError: If an unexpected error occurs during test execution.
    """
    pool = PoolType(pool_type) if isinstance(pool_type, str) else pool_type
    pool_value = pool.value

    print("\n" + "=" * 80)
    print(f"TESTING POOL TYPE: {pool_value.upper()}")
    print("=" * 80)

    redis_host = os.getenv("REDIS_HOST", "localhost")
    redis_url = f"redis://{redis_host}:{redis_port}/0"
    tasks_pool_test.celery_app.conf.broker_url = redis_url
    tasks_pool_test.celery_app.conf.result_backend = redis_url

    # Clean Redis before test to ensure clean state
    cleanup_redis_queue(redis_url)

    # Pass broker URL to worker to ensure both use same configuration
    try:
        with managed_worker(pool_value, concurrency, broker_url=redis_url):
            try:
                wait_for_worker_ready()
                result = run_test_task(pool_type=pool_value)

                if result.get("status") == "success":
                    print(f"\n[RESULT] {pool_value.upper()}: PASSED")
                    print(f"[RESULT] Message: {result.get('message')}")
                    return True
                print(f"\n[RESULT] {pool_value.upper()}: FAILED")
                print(f"[RESULT] Error: {result.get('error', 'Unknown error')}")
                return False

            except RuntimeError as e:
                print(f"\n[RESULT] {pool_value.upper()}: FAILED (task error)")
                print(f"[RESULT] Error: {e}")
                return False
            except KeyboardInterrupt:
                print("\n[INFO] Test interrupted by user")
                return False
    finally:
        # Always clean Redis after test to prevent interference
        cleanup_redis_queue(redis_url)
        # Extra pause for deadlocked workers to be killed
        time.sleep(1)


def validate_pool_type(pool_type: str) -> PoolType:
    """Validate that a pool type is valid.

    Args:
        pool_type: Pool type to validate.

    Returns:
        Validated PoolType enum.

    Raises:
        ValidationError: If pool type is invalid.
    """
    try:
        return PoolType.from_string(pool_type)
    except ValueError as e:
        raise ValidationError(str(e)) from e


def get_pools_to_test(args: argparse.Namespace) -> list[PoolType]:
    """Determine which pool types will be tested based on arguments.

    Args:
        args: Parsed command line arguments.

    Returns:
        List of PoolType enums to test.

    Raises:
        ValidationError: If pool type is invalid.
    """
    if args.all:
        return list[PoolType](PoolType)
    return [validate_pool_type(args.pool)]


def validate_dependencies(pools: list[PoolType]) -> None:
    """Validate dependencies for a list of pool types.

    Args:
        pools: List of pool types to validate dependencies for.

    Raises:
        DependencyError: If any required dependencies are missing.
    """
    missing_deps = []
    for pool_type in pools:
        try:
            check_pool_dependency(pool_type)
        except DependencyError as e:
            missing_deps.append(str(e))

    if missing_deps:
        error_msg = "Missing dependencies:\n  " + "\n  ".join(missing_deps)
        raise DependencyError(error_msg)


def validate_args(args: argparse.Namespace) -> None:
    """Validate command line arguments.

    Args:
        args: Command line arguments.

    Raises:
        ValidationError: If argument validation fails.
        DependencyError: If required dependencies are missing.
    """
    pools_to_test = get_pools_to_test(args)
    validate_dependencies(pools_to_test)


def concurrency_type(value: str) -> int:
    """Type converter for concurrency argument with validation.

    Args:
        value: String value to convert.

    Returns:
        Validated concurrency value.

    Raises:
        argparse.ArgumentTypeError: If value is invalid.
    """
    try:
        concurrency = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"Concurrency must be an integer, got '{value}'"
        ) from e
    if concurrency < MIN_CONCURRENCY:
        raise argparse.ArgumentTypeError(
            f"Concurrency must be at least {MIN_CONCURRENCY}, got {concurrency}"
        )
    if concurrency > MAX_CONCURRENCY:
        raise argparse.ArgumentTypeError(
            f"Concurrency is unreasonably high ({concurrency}). "
            f"Maximum recommended value is {MAX_CONCURRENCY}."
        )
    return concurrency


def redis_port_type(value: str) -> int:
    """Type converter for redis-port argument with validation.

    Args:
        value: String value to convert.

    Returns:
        Validated port number.

    Raises:
        argparse.ArgumentTypeError: If value is invalid.
    """
    try:
        port = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"Redis port must be an integer, got '{value}'"
        ) from e
    if port < MIN_PORT or port > MAX_PORT:
        raise argparse.ArgumentTypeError(
            f"Redis port must be between {MIN_PORT} and {MAX_PORT}, got {port}"
        )
    return port


def get_help_epilog() -> str:
    """Generate the help epilog text with pool types, exit codes, and examples.

    Returns:
        Formatted epilog string for argparse help.
    """
    pool_types_section = "\n".join(
        f"  {pool.value:10s} - {POOL_CONFIGS[pool].description}" for pool in PoolType
    )

    exit_codes_section = "\n".join(
        [
            f"  {ExitCode.SUCCESS} - Success (all tests passed as expected)",
            f"  {ExitCode.GENERAL_ERROR} - General error",
            f"  {ExitCode.VALIDATION_ERROR} - Invalid command line arguments",
            f"  {ExitCode.DEPENDENCY_ERROR} - Missing required dependencies",
            f"  {ExitCode.TEST_FAILED} - Single test failed",
            (
                f"  {ExitCode.UNEXPECTED_TEST_RESULTS} - "
                "Tests didn't match expectations (--all mode)"
            ),
        ]
    )

    sections = [
        "Pool Types:",
        pool_types_section,
        "",
        "Exit Codes:",
        exit_codes_section,
        "",
        "Examples:",
        "  %(prog)s --pool prefork          # Test prefork pool (expects failure)",
        "  %(prog)s --pool gevent           # Test gevent pool (expects success)",
        "  %(prog)s --all                   # Test all pool types",
        "  %(prog)s --concurrency 4         # Test with 4 workers",
        "  %(prog)s --redis-port 6380       # Use specific Redis port",
        "",
        "Usage in Shell Scripts:",
        "  %(prog)s --pool gevent",
        f"  if [ $? -eq {ExitCode.SUCCESS} ]; then",
        '      echo "Test passed!"',
        "  fi",
        "",
    ]

    return "\n".join(sections)


def create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser.

    Returns:
        Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        description="Test Celery worker pools with fork-safety issue",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=get_help_epilog(),
    )
    parser.add_argument(
        "--pool",
        choices=PoolType.choices(),
        default=PoolType.PREFORK.value,
        help=f"Pool type to test (default: {PoolType.PREFORK.value})",
    )
    parser.add_argument(
        "--concurrency",
        type=concurrency_type,
        default=DEFAULT_CONCURRENCY,
        help=(
            f"Number of concurrent workers "
            f"(default: {DEFAULT_CONCURRENCY}, max: {MAX_CONCURRENCY})"
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run all pool types sequentially",
    )
    parser.add_argument(
        "--redis-port",
        type=redis_port_type,
        default=None,
        help=(
            f"Redis port (default: auto-detect {DEFAULT_REDIS_PORTS}, "
            f"range: {MIN_PORT}-{MAX_PORT})"
        ),
    )

    return parser


def run_all_pool_tests(concurrency: int, redis_port: int) -> dict[PoolType, bool]:
    """Run tests for all pool types.

    Args:
        concurrency: Number of concurrent workers.
        redis_port: Redis port to use.

    Returns:
        Dictionary mapping pool types to test results.
    """
    print("\n" + "=" * 80)
    print("RUNNING A/B TEST: ALL POOL TYPES")
    print("=" * 80)

    results: dict[PoolType, bool] = {}

    for pool_type in PoolType:
        config = POOL_CONFIGS[pool_type]
        print(f"\n[INFO] Testing {config.description}")
        results[pool_type] = test_pool(pool_type, concurrency, redis_port)
        time.sleep(TEST_PAUSE_SECONDS)

    return results


def print_test_summary(results: dict[PoolType, bool]) -> bool:
    """Print test summary and determine if all tests match expectations.

    Args:
        results: Dictionary mapping pool types to test results.

    Returns:
        True if all tests matched expectations, False otherwise.
    """
    print("\n" + "=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)

    all_correct = True
    for pool_type, passed in results.items():
        config = POOL_CONFIGS[pool_type]
        status = "PASSED" if passed else "FAILED"
        expected = "PASS" if config.expected_to_pass else "FAIL"
        matches = passed == config.expected_to_pass

        match_indicator = "[OK]" if matches else "[MISMATCH]"
        print(
            f"{pool_type.value:10s}: {status:6s} "
            f"(Expected: {expected:4s}) {match_indicator}"
        )

        if not matches:
            all_correct = False

    return all_correct


def main() -> int:
    """Main entry point.

    Returns:
        int: Exit code (0 for success, non-zero for various failures).
            See ExitCode enum for specific exit codes.
    """
    parser = create_argument_parser()
    args = parser.parse_args()

    try:
        validate_args(args)
    except ValidationError as e:
        parser.error(str(e))
    except DependencyError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        print("\nTo install missing dependencies, run:", file=sys.stderr)
        print("  pip install gevent eventlet", file=sys.stderr)
        return ExitCode.DEPENDENCY_ERROR

    try:
        redis_port_found = check_redis(args.redis_port)
    except RuntimeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return ExitCode.GENERAL_ERROR

    if args.all:
        results = run_all_pool_tests(args.concurrency, redis_port_found)
        all_correct = print_test_summary(results)

        if all_correct:
            print("\n[SUCCESS] All tests match expectations!")
            return ExitCode.SUCCESS

        print("\n[WARNING] Some tests don't match expectations")
        return ExitCode.UNEXPECTED_TEST_RESULTS

    passed = test_pool(args.pool, args.concurrency, redis_port_found)
    return ExitCode.SUCCESS if passed else ExitCode.TEST_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
