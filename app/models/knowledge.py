"""Knowledge base domain models (2 tables).

Uses pgvector Vector type when running on PostgreSQL and pgvector is installed.
Falls back to JSON storage of the embedding list otherwise (SQLite / EXE).
"""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, JSON, func

from app.core.config import settings
from app.db.base import Base

def _embedding_type():
    """pgvector `vector(N)` when available, JSON otherwise.

    EMBEDDING_STORAGE lets an installation opt out explicitly: the Windows offline
    build ships PostgreSQL binaries and compiles pgvector next to them, but if that
    extension cannot be built on a given build machine the installer sets
    EMBEDDING_STORAGE=json so a PostgreSQL install still starts — knowledge-base
    retrieval then uses the same application-side cosine path as SQLite, instead of
    failing at CREATE TABLE time with "type vector does not exist".
    """
    if settings.is_sqlite:
        return JSON
    mode = (getattr(settings, "EMBEDDING_STORAGE", "auto") or "auto").strip().lower()
    if mode == "json":
        return JSON
    try:
        from pgvector.sqlalchemy import Vector

        return Vector(settings.AI_EMBEDDING_DIM)
    except Exception:
        if mode == "vector":
            raise
        return JSON


EmbeddingType = _embedding_type()


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    doc_type = Column(String(30))
    file_path = Column(String(500))
    device_model_id = Column(Integer, ForeignKey("device_models.id"))
    uploaded_by = Column(Integer, ForeignKey("users.id"))
    status = Column(String(20), default="processing")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False)
    chunk_index = Column(Integer, nullable=False, default=0)
    content = Column(Text, nullable=False)
    embedding = Column(EmbeddingType)  # Vector on Postgres, JSON otherwise
    created_at = Column(DateTime(timezone=True), server_default=func.now())
