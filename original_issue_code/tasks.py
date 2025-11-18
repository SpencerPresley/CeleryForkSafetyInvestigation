from celery import shared_task
from langchain_chroma import Chroma
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_core.documents import Document
from uuid import uuid4
import os
from celery_app import celery_app

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "models/embedding-001")
EMBEDDING_PROVIDER_API_KEY = os.getenv("GOOGLE_API_KEY")

embedding = GoogleGenerativeAIEmbeddings(
    model=EMBEDDING_MODEL,
    google_api_key=EMBEDDING_PROVIDER_API_KEY,
)

vector_store = Chroma(
    collection_name="example_collection",
    embedding_function=embedding,
    persist_directory="./chroma_db",
)

@celery_app.task(name="tasks.add_documents_task")
def add_documents_task(docs: list[dict]):
    documents = [Document(**doc) for doc in docs]
    uuids = [str(uuid4()) for _ in range(len(documents))]
    vector_store.add_documents(documents=documents, ids=uuids)
    print("document ingestion is done.")
    return f"Inserted {len(documents)} documents"