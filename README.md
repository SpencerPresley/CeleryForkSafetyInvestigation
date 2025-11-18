# Fork-Safety Bug Demonstration

This repository demonstrates a fork-safety issue that manifests when database connections or other fork-unsafe resources are initialized before `fork()` in multiprocessing environments, using ChromaDB/SQLite as an example.

**Inspired by:** LangChain issue [#33246](https://github.com/langchain-ai/langchain/issues/33246) - "Document ingestion into VectorDB does not complete when running inside Celery task" by [@BennisonDevadoss](https://github.com/BennisonDevadoss)

>[!NOTE] 
>**Disclaimer:**
>
> I am not claiming to be an expert in any of the things mentioned in this repository, particularly the low level instructions and registers. I did my best to research the problem and cite sources as best I could.
>
> That being said, regardless of the low-level details, I'm fairly confident in what causes the issue and solutions.

## Table of Contents

- [Repository Structure](#repository-structure)
- [The Problem](#the-problem)
- [What This Actually Is](#what-this-actually-is)
- [On `fork()` and multi-threaded environments](#on-fork-and-multi-threaded-environments)
- [How this applies to this problem](#how-this-applies-to-this-problem)
- [Testing and Platform Behavior Details](#testing-and-platform-behavior-details)
- [Quick Start](#quick-start)
  - [Docker](#docker-recommended-for-linux-behavior-testing)
  - [Native](#native-required-for-macos-sigtrap-testing)
- [Available Commands](#available-commands)
  - [Docker Commands](#docker-commands-via-make)
  - [Native Commands](#native-commands)
- [Output Directories](#output-directories)
- [Fixes/Solutions](#fixessolutions)
- [Environment Setup](#environment-setup)
- [Docker Details](#docker-details)
- [Troubleshooting](#troubleshooting)
- [Sources](#sources)

## Repository Structure

```
celery_examples/          # Celery pool comparison tests
├── run_test.py          # Test runner
└── tasks_pool_test.py   # Celery tasks (fork-unsafe pattern)

scripts/                 # Demo crash scripts
├── demo_crash.py        # Demonstrates the bug
├── gdb_attach_child.py  # Linux debugger (Docker)
└── lldb_attach_child.py # macOS debugger (native)

docs/                    # Detailed analysis
├── LINUX_RESULTS.md     # GDB debugging output
├── MACOS_RESULTS.md     # LLDB debugging output
```

## The Problem

When a process with active database connections forks, the child process inherits mutex/semaphore state that references threads from the parent process. These threads don't exist in the child, causing platform-specific failures. On Linux it manifests as a deadlock and on macOS as a crash due to SIGTRAP being raised.

## What This Actually Is

This is **not a ChromaDB bug**. It occurs as a side effect of **Celery's** default pooling strategy of `prefork` being used in a multi-threaded environment. When using the `prefork` pool, celery uses [Billiard](https://github.com/celery/billiard) under the hood which uses `fork()` via `os.fork()` to create child processes. This is typically fine, but `fork()` has limitations when used with multi-threaded programs.

## On `fork()` and multi-threaded environments

Using `fork()` when a process has multiple threads causes only the calling thread to exist in the child process. However, the child inherits a copy of the entire address space, including the states of mutexes and other synchronization primitives. Since the threads that held these locks no longer exist in the child, any attempt to acquire these locks results in deadlock or in the case of MacOS, a [SIGTRAP](https://man7.org/linux/man-pages/man7/signal.7.html) being raised due to the detection of corrupted semaphores.

This behavior is documented in both the POSIX specification and Linux man pages:

From the [POSIX fork() specification](https://pubs.opengroup.org/onlinepubs/009696799/functions/fork.html):
> "A process shall be created with a single thread. If a multi-threaded process calls fork(), the new process shall contain a replica of the calling thread and its entire address space, possibly including the states of mutexes and other resources."

This means that when you fork a multi-threaded process, only ONE thread is copied to the child. But the memory is copied as-is, meaning mutexes that were locked by other threads are still marked as "locked" even though those threads don't exist anymore. The child process has no way to unlock them.

From the [Linux fork(2) man page](https://man7.org/linux/man-pages/man2/fork.2.html):
> "The child process is created with a single thread—the one that called fork().  The entire virtual address space of the parent is replicated in the child, including the states of mutexes, condition variables, and other pthreads objects."

This means that the child gets copies of all the synchronization primitives (mutexes, condition variables) in whatever state they were in when fork() was called. If a mutex was locked in the parent by a thread that doesn't exist in the child, that mutex will remain locked forever in the child process. 

## How this applies to this problem

In our problem we:
- Use Celery with the `prefork` pool which uses `fork()` to create child processes
- Use ChromaDB which uses Rust with the Tokio runtime and SQLite
  - Tokio runtime is not fork-safe
  - SQLite is not fork-safe
- Initialize ChromaDB at module level
- Have a task that uses the ChromaDB instance to add documents

In the original issue, the code looked like this:

```python
# tasks.py
from langchain_chroma import Chroma
from langchain_google_genai import GoogleGenerativeAIEmbeddings

# Module level initialization (BEFORE fork)
embedding = GoogleGenerativeAIEmbeddings(...)
vector_store = Chroma(
    collection_name="example_collection",
    embedding_function=embedding,
    persist_directory="./chroma_db",
)

@celery_app.task
def add_documents_task(docs):
    vector_store.add_documents(...)  # Uses the module level vector_store
```

The following sequence of events occurs:

1. **Start Celery worker** (`celery -A celery_app worker`)
   - Worker process starts
   - Imports `tasks.py` module
   - **Module level code executes:** `vector_store = Chroma(...)`
     - ChromaDB initializes Tokio runtime (creates thread pools with mutexes)
     - ChromaDB opens SQLite connection (creates mutexes for connection state)
   - Worker is ready, waiting for tasks

2. **Start FastAPI server** (`uvicorn main:app`)
   - Server starts and waits for requests

3. **Request sent to endpoint** (`GET /add-docs`)
   - FastAPI calls `add_documents_task.delay(docs)`
   - Task is queued to Celery broker (Redis in this case)
   - Celery worker picks up the task

4. **Celery forks a child process** (prefork pool)
   - At some point in the task execution, `fork()` is called to create worker process
   - Child inherits memory with ChromaDB's mutexes and semaphores
   - **Only the calling thread exists in child** - all other threads are gone ([Linux proof](docs/LINUX_RESULTS.md#L181), [macOS proof](docs/MACOS_RESULTS.md#L156-L195))
   - Mutexes/semaphores are still marked "locked" by threads that no longer exist

5. **Child process tries to execute task**
   - Calls `vector_store.add_documents(...)`
   - Attempts to acquire Tokio/SQLite mutexes
   - **Linux:** Deadlock (waits forever for non-existent thread to release lock)
   - **macOS:** SIGTRAP (libdispatch detects corrupted semaphore state)

The critical element is ChromaDB being initialized at module level when using Celery with pool type `prefork`. Module level initialization means ChromaDB (and its internal Tokio runtime and SQLite connections) are created **before** `fork()` happens. When `fork()` is called, the child process inherits these already-initialized objects with their mutexes/semaphores in whatever state they were in - potentially locked by threads that no longer exist in the child.

When the child process attempts to use ChromaDB, it tries to acquire these inherited mutexes/semaphores. On **Linux**, the process calls [`futex()` system call](docs/LINUX_RESULTS.md#L58-L84) and waits indefinitely. The kernel will never return because no thread exists to release the lock. On **macOS**, the process attempts to signal a GCD semaphore, but [libdispatch detects the corrupted state](docs/MACOS_RESULTS.md#L88-L96) and immediately aborts with EXC_BREAKPOINT via a [hardware breakpoint instruction (`brk #0x1`)](docs/MACOS_RESULTS.md#L209).

tl;dr:

**When `fork()` creates a child process:**
- Memory is copied (including mutex states)
- Threads are NOT copied (only the calling thread exists in child)
- Mutexes/semaphores appear "locked" by threads that don't exist
- The process hangs indefinitely on Linux or crashes on macOS

**This can affect any library that uses multi-threading or maintains internal state with synchronization primitives:**
- Database connections (SQLite, PostgreSQL, MySQL)
- Async runtimes (Tokio, asyncio with threads)
- Thread pools or background workers
- Network connections with internal state

Some additional quotes from SQLite and psycopg3 docs which speak to this:

From [SQLite FAQ](https://www.sqlite.org/faq.html#q6):
> "Do not open an SQLite database connection, then fork(), then try to use that database connection in the child process."

From [psycopg3 (PostgreSQL) documentation](https://www.psycopg.org/psycopg3/docs/advanced/pool.html):
> "If you are using Psycopg in a forking framework, make sure that the database connections are created after the worker process is forked."

## Testing and Platform Behavior Details

The following table summarizes the behavior of the fork-safety bug across platforms as well as states if it can be tested in Docker. This table is based on my own testing and is not exhaustive.

| Platform | Architecture | Behavior | Debugger | Can Test in Docker? |
|----------|-------------|----------|----------|---------------------|
| **Linux** | x86_64 | Deadlock (futex) | GDB | Yes |
| **Linux** | ARM64 | Deadlock (futex) | GDB | Yes |
| **macOS** | Intel | SIGTRAP crash | LLDB | No - Run natively |
| **macOS** | Apple Silicon | SIGTRAP crash | LLDB | No - Run natively |

> [!IMPORTANT]
> **Tested on:** Linux ARM64 and macOS Apple Silicon only. Behavior appears to be OS-dependent, not CPU architecture-dependent due to ARM Linux behaving the same as x86_64 Linux. Therefore, x86_64/Intel behavior is expected to be the same as ARM64 Linux and Apple Silicon macOS, but I have not personally tested it yet, so take x86_64/Intel behavior with a grain of salt.

## Fixes/Solutions

- **Initialize after fork:** Create database connections within tasks, not at module level
  - Such as in this case initializing the ChromaDB instance inside the task function
- **Use non-forking pools:** Use `gevent`, `eventlet`, or `threads` as the pool type instead of `prefork`
  - gevent and eventlet don't use `fork()`
  - threads pool uses shared memory space so no fork-safety issue

## Instructions for Reproducing the Issue

### Docker (Recommended for Linux behavior testing)

**With Makefile:**

```bash
# Build and start Redis
make build
make redis

# Run all Celery pool tests
make test-all

# Run crash demo with GDB debugging
make demo-gdb
```

**Without Makefile:**
```bash
# Build and start Redis
docker compose build
docker compose up -d redis

# Run all Celery pool tests
docker compose run --rm celery-test-all

# Run crash demo with GDB debugging
docker compose run --rm demo-gdb
```

### Native (Required for macOS SIGTRAP testing)

```bash
# Install dependencies
uv sync
source .venv/bin/activate

# Copy and configure environment
cp .env.example .env
# Edit .env with your API keys

# Run tests
python celery_examples/run_test.py --all

# Demo crash (macOS shows SIGTRAP)
python scripts/demo_crash.py

# Debug with LLDB (macOS only)
python scripts/lldb_attach_child.py
```

## Available Commands

### Docker Commands (via Make)

**Testing:**
- `make test-all` - Test all pool types (prefork, gevent, eventlet, threads)
- `make test-prefork` - Test prefork pool (expects FAILURE)
- `make test-gevent` - Test gevent pool (expects SUCCESS)
- `make test-eventlet` - Test eventlet pool (expects SUCCESS)
- `make test-threads` - Test threads pool (expects SUCCESS)

**Demos:**
- `make demo-crash` - Run crash demo (normal)
- `make demo-gdb` - Run crash demo with GDB debugging
- `make demo-strace` - Run crash demo with strace
- `make demo-fixed` - Run fixed version (no crash)

**Utilities:**
- `make shell` - Interactive shell with Redis
- `make clean` - Clean output directories
- `make down` - Stop all services

### Native Commands

```bash
# Test all pools
python celery_examples/run_test.py --all

# Test specific pool
python celery_examples/run_test.py --pool prefork  # FAIL
python celery_examples/run_test.py --pool gevent   # PASS

# Demo crash
python scripts/demo_crash.py

# macOS debugging with LLDB
python scripts/lldb_attach_child.py

# Linux debugging (requires Docker)
make demo-gdb
```

## Output Directories

After running tests/demos:
- `crash_dumps/` - Logs, GDB output, strace traces
- `chroma_db_demo_crash/` - ChromaDB data from demo
- `chroma_db_pool_test/` - ChromaDB data from Celery tests

## Environment Setup

Create `.env` from template:

```bash
cp .env.example .env
```

Required variables:
- `PROVIDER` - `openai` or `google`
- `API_KEY` - Your API key
- `EMBEDDING_MODEL` - Model name for embeddings

## Docker Details

The project uses a single unified `compose.yml` with:
- Single Dockerfile using uv for fast dependency installation
- Redis service for Celery broker/backend
- Multiple service definitions for different test scenarios
- Shared volumes for output persistence

All commands use `docker compose` (Docker Compose V2 standard).

## Troubleshooting

**Redis connection failed:**
```bash
# Start Redis first
make redis

# Or use docker compose directly
docker compose up -d redis
```

**Permission denied on output directories:**
```bash
chmod 777 crash_dumps chroma_db_*
```

**GDB not working:**
```bash
# Ensure you're using Docker (GDB doesn't work well on macOS)
make demo-gdb
```

## Sources

**POSIX/System Documentation:**
- [POSIX fork() specification](https://pubs.opengroup.org/onlinepubs/009696799/functions/fork.html)
- [Linux fork(2) man page](https://man7.org/linux/man-pages/man2/fork.2.html)
- [Linux futex(2) man page](https://man7.org/linux/man-pages/man2/futex.2.html)
- [Linux signal(7) man page](https://man7.org/linux/man-pages/man7/signal.7.html) (SIGTRAP)

**Library Documentation:**
- [Celery documentation](https://docs.celeryq.dev/)
- [Billiard (Celery's multiprocessing library)](https://github.com/celery/billiard)
- [gevent documentation](https://www.gevent.org/)
- [eventlet documentation](https://eventlet.readthedocs.io/)
- [SQLite FAQ - Fork safety](https://www.sqlite.org/faq.html#q6)
- [psycopg3 (PostgreSQL) - Pool and fork safety](https://www.psycopg.org/psycopg3/docs/advanced/pool.html)
- [ChromaDB documentation](https://docs.trychroma.com/)

**Debugging Tools:**
- [GDB documentation](https://www.gnu.org/software/gdb/documentation/)
- [LLDB documentation](https://lldb.llvm.org/)
- [strace man page](https://man7.org/linux/man-pages/man1/strace.1.html)

**Inspiration:**
- [LangChain issue #33246](https://github.com/langchain-ai/langchain/issues/33246) by [@BennisonDevadoss](https://github.com/BennisonDevadoss)