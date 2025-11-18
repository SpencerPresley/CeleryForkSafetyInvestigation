"""Celery tasks for pool comparison test.

This module uses the BROKEN pattern (module-level initialization)
to test whether different Celery worker pools avoid the fork-safety issue.

This should:
- FAIL with prefork pool (fork happens, orphaned lock state)
- WORK with gevent/eventlet pools (no fork, cooperative multitasking)
"""

import logging
import os
from pathlib import Path
from uuid import uuid4

from celery import Celery
from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_openai import OpenAIEmbeddings

logger = logging.getLogger(__name__)

# Load .env - try parent directory first (Docker), then current directory (local)
env_file = Path(__file__).parent.parent / ".env"
if not env_file.exists():
    env_file = Path.cwd() / ".env"
load_dotenv(env_file)

# Check for CELERY_BROKER_URL first (set by parent process),
# then fall back to REDIS_HOST/REDIS_PORT
_broker_url = os.getenv("CELERY_BROKER_URL")
if _broker_url:
    _result_backend = os.getenv("CELERY_RESULT_BACKEND", _broker_url)
else:
    _redis_host = os.getenv("REDIS_HOST", "localhost")
    _redis_port = os.getenv("REDIS_PORT", "6379")
    _broker_url = f"redis://{_redis_host}:{_redis_port}/0"
    _result_backend = _broker_url

celery_app = Celery(
    "pool_test",
    broker=_broker_url,
    backend=_result_backend,
)

# Explicitly set task imports - this ensures the module is imported
# when the worker starts, making tasks available for execution
celery_app.conf.update(
    imports=["tasks_pool_test"],
    task_always_eager=False,
    task_eager_propagates=False,
)

PROVIDER = os.getenv("PROVIDER")
if not PROVIDER:
    raise ValueError("PROVIDER is not set")

API_KEY = os.getenv("API_KEY")
if not API_KEY:
    raise ValueError("API_KEY is not set")

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL")
if not EMBEDDING_MODEL:
    raise ValueError("EMBEDDING_MODEL is not set")

print(f"[MODULE IMPORT] tasks_pool_test.py imported in PID {os.getpid()}")

embedding_test = None
if PROVIDER == "openai":
    embedding_test = OpenAIEmbeddings(
        model=EMBEDDING_MODEL,
        api_key=API_KEY,
    )
elif PROVIDER == "google":
    embedding_test = GoogleGenerativeAIEmbeddings(
        model=EMBEDDING_MODEL,
        google_api_key=API_KEY,
    )

# Use Docker path if /app exists (Docker environment), otherwise use local path
if Path("/app").exists():
    chroma_persist_dir = "/app/chroma_db_pool_test"
else:
    # Local development - use a directory relative to the script
    chroma_persist_dir = str(Path(__file__).parent.parent / "chroma_db_pool_test")

vector_store_test = Chroma(
    collection_name="pool_test_collection",
    embedding_function=embedding_test,
    persist_directory=chroma_persist_dir,
)


@celery_app.task(name="tasks_pool_test.add_documents")
def add_documents(docs: list[dict]):
    """Test task using fork-unsafe pattern (module-level initialization).

    This should:
    - Fail: Deadlock/SIGTRAP with prefork pool
    - Pass: No deadlock/SIGTRAP with gevent/eventlet/threads pools

    Args:
        docs: List of document dictionaries with page_content and metadata.

    Returns:
        dict: Result with status, PID info, and document count.
    """
    current_pid = os.getpid()
    parent_pid = os.getppid()

    print(f"[TASK] Executing in PID {current_pid}, parent PID {parent_pid}")
    print("[TASK] Using module-level vector_store (BROKEN pattern)")

    try:
        documents = [Document(**doc) for doc in docs]
        uuids = [str(uuid4()) for _ in range(len(documents))]

        print(f"[TASK] Attempting to add {len(documents)} documents...")
        vector_store_test.add_documents(documents=documents, ids=uuids)

        print("[TASK] SUCCESS! Documents added successfully")

        return {
            "status": "success",
            "pid": current_pid,
            "parent_pid": parent_pid,
            "documents_inserted": len(documents),
            "pattern": "module_level_initialization",
            "message": "Task completed successfully - pool avoided fork-safety issue",
        }
    except Exception as e:
        logger.exception("[TASK] ERROR: %s", e)
        return {
            "status": "error",
            "pid": current_pid,
            "parent_pid": parent_pid,
            "error": str(e),
            "pattern": "module_level_initialization",
            "message": f"Task failed - likely fork-safety issue: {e}",
        }
