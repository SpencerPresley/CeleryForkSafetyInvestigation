from celery import Celery

BROKER_HOST="postgresql://<username>@localhost:5432/agentic_rag"
DATABASE_URL="postgresql://<username>@localhost:5432/agentic_rag"

celery_app = Celery(
    "tasks",
    broker="redis://localhost:6379/0",   # or RabbitMQ URL
    backend=f"db+{DATABASE_URL}",
)

celery_app.conf.task_routes = {
    "tasks.add_documents_task": {"queue": "documents"},
}

import tasks