"""Knowledge base domain models (2 tables).

Uses pgvector Vector type when running on PostgreSQL and pgvector is installed.
Falls back to JSON storage of the embedding list otherwise (SQLite / EXE).
"""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, JSON, func

from app.core.config import settings
from app.db.base import Base

EmbeddingType = JSON  # safe default for EXE / SQLite
if not settings.is_sqlite:
    try:
        from pgvector.sqlalchemy import Vector
        EmbeddingType = Vector(settings.AI_EMBEDDING_DIM)
    except Exception:
        EmbeddingType = JSON


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
