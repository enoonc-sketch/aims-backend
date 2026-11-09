from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class KnowledgeDocumentCreate(BaseModel):
    title: str
    doc_type: Optional[str] = None
    file_path: Optional[str] = None
    device_model_id: Optional[int] = None
    uploaded_by: Optional[int] = None


class KnowledgeDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    doc_type: Optional[str] = None
    file_path: Optional[str] = None
    device_model_id: Optional[int] = None
    uploaded_by: Optional[int] = None
    status: Optional[str] = None
    created_at: Optional[datetime] = None


class KnowledgeChunkCreate(BaseModel):
    document_id: int
    chunk_index: int = 0
    content: str


class KnowledgeChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    document_id: int
    chunk_index: int
    content: str
    created_at: Optional[datetime] = None


class KnowledgeSearchResult(BaseModel):
    chunk_id: int
    document_id: int
    document_title: Optional[str] = None
    content: str
    score: float = 0.0
