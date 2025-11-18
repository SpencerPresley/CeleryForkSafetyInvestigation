from fastapi import FastAPI

app = FastAPI()

@app.get("/add-docs")
async def add_docs():
    from tasks import add_documents_task
    docs = [
        {"page_content": "LangGraph is awesome!", "metadata": {"source": "tweet"}},
        {"page_content": "The stock market fell today.", "metadata": {"source": "news"}},
    ]
    task = add_documents_task.delay(docs)
    return {"task_id": task.id, "status": "queued"}