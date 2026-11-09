"""Knowledge API — documents, chunks, simple full-text search (RAG-ready)."""
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.pagination import LimitQuery, SkipQuery
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.models.system import User
from app.schemas.imports import TableImportResult
from app.schemas.knowledge import (
    KnowledgeChunkCreate,
    KnowledgeChunkOut,
    KnowledgeDocumentCreate,
    KnowledgeDocumentOut,
    KnowledgeSearchResult,
)
from app.services import table_import
from app.services.upload_security import read_upload

router = APIRouter(prefix="/knowledge", tags=["Knowledge"])

_IMPORT_SPEC = {
    "title": ("title", "标题", "文档标题", "名称"),
    "content": ("content", "内容", "正文", "content_text", "text"),
    "doc_type": ("doc_type", "type", "类型", "文档类型"),
}


@router.get("/documents", response_model=list[KnowledgeDocumentOut])
def list_documents(
    db: Session = Depends(get_db),
    status: Optional[str] = None,
    skip: int = SkipQuery,
    limit: int = LimitQuery(50),
):
    stmt = select(KnowledgeDocument)
    if status:
        stmt = stmt.where(KnowledgeDocument.status == status)
    stmt = stmt.order_by(KnowledgeDocument.id.desc()).offset(skip).limit(limit)
    return db.scalars(stmt).all()


@router.post("/documents", response_model=KnowledgeDocumentOut, status_code=201)
def create_document(payload: KnowledgeDocumentCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = KnowledgeDocument(**payload.model_dump(), status="ready")
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@router.get("/documents/{doc_id}", response_model=KnowledgeDocumentOut)
def get_document(doc_id: int, db: Session = Depends(get_db)):
    doc = db.get(KnowledgeDocument, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.delete("/documents/{doc_id}", status_code=204)
def delete_document(doc_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = db.get(KnowledgeDocument, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    db.delete(doc)
    db.commit()


@router.get("/documents/import/template")
def download_documents_import_template():
    """Downloadable .xlsx template for 知识库 -> 批量导入."""
    content = table_import.build_excel_template(
        ["标题", "内容", "类型"],
        [["色谱仪常见故障", "色谱仪载气压力过低会导致基线漂移，需检查减压阀与管路密封。", "SOP"]],
    )
    return Response(
        content=content,
        media_type=table_import.TEMPLATE_MEDIA_TYPE,
        headers=table_import.template_headers(filename="知识库导入模板.xlsx"),
    )


@router.post("/documents/import", response_model=TableImportResult, status_code=201)
async def import_documents(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Bulk-import knowledge documents from an uploaded CSV/Excel file
    (知识库 -> 批量导入). Each row becomes one KnowledgeDocument (title/doc_type)
    plus a single KnowledgeChunk holding that row's `content` column, embedded
    with the same real/local embedding model (or bag-of-words fallback) used
    by /knowledge/search and AI chat RAG — so imported rows are searchable
    and chat-answerable immediately, no separate "upload" step needed.
    """
    upload = await read_upload(file)  # name sanitising, ext whitelist, capped read, content sniffing, audit note
    raw, filename = upload.raw, upload.filename

    try:
        df = table_import.read_table(raw, filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if df.empty:
        raise HTTPException(status_code=400, detail="File has no data rows")

    columns_detected = table_import.detect_columns(df.columns, _IMPORT_SPEC)
    if "content" not in columns_detected:
        raise HTTPException(status_code=400, detail="Could not detect required column content (内容/正文)")

    warnings: list[str] = []
    rows_to_insert: list[tuple[str, Optional[str], str]] = []  # (title, doc_type, content)

    for i in range(len(df)):
        row = df.iloc[i]
        content = table_import.clean_str(table_import.row_value(row, columns_detected, "content"))
        if not content:
            warnings.append(f"Row {i + 2}: empty content — skipped")
            continue
        title = table_import.clean_str(table_import.row_value(row, columns_detected, "title"))
        if not title:
            title = content[:50] + ("…" if len(content) > 50 else "")
        doc_type = table_import.clean_str(table_import.row_value(row, columns_detected, "doc_type"))
        rows_to_insert.append((title, doc_type, content))

    inserted = 0
    if rows_to_insert:
        from app.services.rag import embed_texts

        try:
            contents = [c for _, _, c in rows_to_insert]
            vectors = await embed_texts(contents, db=db)
            for idx, (title, doc_type, content) in enumerate(rows_to_insert):
                doc = KnowledgeDocument(title=title, doc_type=doc_type, status="ready")
                db.add(doc)
                db.flush()
                chunk = KnowledgeChunk(document_id=doc.id, chunk_index=0, content=content)
                if vectors:
                    chunk.embedding = vectors[idx]
                db.add(chunk)
                inserted += 1
            db.commit()
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=400, detail=f"Import failed while embedding/saving documents: {e}")

    return TableImportResult(
        file_name=filename,
        row_count=len(df),
        columns_detected=columns_detected,
        inserted=inserted,
        updated=0,
        skipped=len(df) - inserted,
        status="done",
        warnings=warnings,
    )


@router.post("/chunks", response_model=KnowledgeChunkOut, status_code=201)
async def create_chunk(payload: KnowledgeChunkCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = db.get(KnowledgeDocument, payload.document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    data = payload.model_dump()
    # optional embedding
    from app.services.rag import embed_texts
    vectors = await embed_texts([payload.content], db=db)
    if vectors:
        data["embedding"] = vectors[0]
    chunk = KnowledgeChunk(**data)
    db.add(chunk)
    db.commit()
    db.refresh(chunk)
    return chunk


@router.get("/documents/{doc_id}/chunks", response_model=list[KnowledgeChunkOut])
def list_chunks(doc_id: int, db: Session = Depends(get_db)):
    return db.scalars(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.document_id == doc_id)
        .order_by(KnowledgeChunk.chunk_index)
    ).all()


@router.get("/search", response_model=list[KnowledgeSearchResult])
async def search_knowledge(
    q: str = Query(..., min_length=1, description="Semantic / keyword search over knowledge chunks"),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """Hybrid search: real embedding-model similarity when a local model is downloaded (or AI_API_KEY set), else bag-of-words cosine."""
    from app.services.rag import embed_texts, search_chunks
    emb = None
    vectors = await embed_texts([q], db=db)
    if vectors:
        emb = vectors[0]
    hits = search_chunks(db, q, limit=limit, query_embedding=emb)
    return [
        KnowledgeSearchResult(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            document_title=title,
            content=chunk.content,
            score=round(score, 4),
        )
        for chunk, title, score in hits
    ]
