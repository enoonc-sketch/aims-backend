"""Lightweight RAG helpers.

- Offline: keyword + simple bag-of-words cosine similarity over knowledge_chunks
- Optional: call OpenAI-compatible embeddings when AI_API_KEY is set, store vectors
"""
from __future__ import annotations

import math
import re
from typing import Optional

import httpx
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument


_TOKEN = re.compile(r"[a-zA-Z0-9\u4e00-\u9fff]+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text or "") if len(t) > 1]


def _bow(text: str) -> dict[str, float]:
    tokens = _tokenize(text)
    if not tokens:
        return {}
    freq: dict[str, float] = {}
    for t in tokens:
        freq[t] = freq.get(t, 0.0) + 1.0
    # l2 normalize
    norm = math.sqrt(sum(v * v for v in freq.values())) or 1.0
    return {k: v / norm for k, v in freq.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    # iterate smaller
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


async def embed_texts(texts: list[str], db: Optional[Session] = None) -> Optional[list[list[float]]]:
    """Real embedding vectors: local GGUF embedding model first (if downloaded),
    then OpenAI-compatible API, else None (caller falls back to bag-of-words)."""
    if not texts:
        return None

    if db is not None:
        from starlette.concurrency import run_in_threadpool
        from app.services import llm_local

        local_vecs = await run_in_threadpool(llm_local.embed_local, db, texts)
        if local_vecs:
            return local_vecs

    if not settings.AI_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{settings.AI_API_BASE}/embeddings",
                headers={"Authorization": f"Bearer {settings.AI_API_KEY}"},
                json={"model": settings.AI_EMBEDDING_MODEL, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
            # ensure sorted by index
            items = sorted(data["data"], key=lambda x: x["index"])
            return [it["embedding"] for it in items]
    except Exception:
        return None


def _vec_cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _fts_match_expr(query: str, max_terms: int = 24) -> Optional[str]:
    """Build an FTS5 MATCH expression for the knowledge_chunks_fts table.

    The table uses the `trigram` tokenizer (not the default unicode61),
    because unicode61 never splits CJK text into words — a whole run of
    Chinese characters becomes a single token, so word-level OR queries
    never match. Trigram indexes every overlapping 3-character window
    instead, which supports substring search for Chinese (and still works
    for English). Its one limitation: a MATCH term must be >= 3 characters,
    so we slide a 3-char window over the (whitespace-stripped) query rather
    than OR-ing individual words/characters.
    """
    raw = re.sub(r"\s+", "", query)
    terms: set[str] = set()
    for i in range(len(raw) - 2):
        terms.add(raw[i:i + 3])
        if len(terms) >= max_terms:
            break
    # also keep whole latin/number tokens (>=3 chars) verbatim for exact hits
    terms.update(t for t in re.findall(r"[a-zA-Z0-9]{3,}", query.lower()))
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms)


def _sqlite_fts_candidates(db: Session, query: str, limit: int) -> Optional[dict[int, float]]:
    """Narrow the candidate set via the knowledge_chunks_fts virtual table
    (SQLite only), returning {chunk_id: relevance_score} ordered by bm25.

    This also stands in for the bag-of-words score on Chinese content: since
    _tokenize/_bow treat a whole run of CJK characters as a single token
    (no word segmentation), the cosine-similarity path below often scores
    genuine Chinese matches as 0. FTS's trigram-based rank doesn't have that
    problem, so callers should blend it in rather than only using it to
    pre-filter rows.

    Returns None (caller falls back to a full scan / pure bow scoring) when
    SQLite's FTS5 module or the fts table isn't available — e.g. a DB
    created before this feature, or a Python sqlite3 build without FTS5.
    """
    if not settings.is_sqlite:
        return None
    match_expr = _fts_match_expr(query)
    if not match_expr:
        return None
    try:
        rows = db.execute(
            text(
                "SELECT rowid FROM knowledge_chunks_fts "
                "WHERE knowledge_chunks_fts MATCH :expr "
                "ORDER BY bm25(knowledge_chunks_fts) LIMIT :limit"
            ),
            {"expr": match_expr, "limit": limit},
        ).all()
        ids = [r[0] for r in rows]
        # Turn bm25 rank order into a descending pseudo-score in (0, 1] so it
        # can be compared/blended with cosine similarity scores below.
        n = len(ids)
        return {cid: 1.0 - 0.5 * (i / n) for i, cid in enumerate(ids)} if n else {}
    except Exception:
        return None


def search_chunks(
    db: Session,
    query: str,
    limit: int = 5,
    query_embedding: Optional[list[float]] = None,
) -> list[tuple[KnowledgeChunk, str, float]]:
    """Return list of (chunk, document_title, score) sorted by relevance desc."""
    # On SQLite, use FTS5 to pre-filter candidates (cheap) instead of loading
    # and scoring every chunk in the knowledge base in Python, and to supply
    # a relevance score for Chinese content where bow scoring alone tends to
    # come back 0 (see _sqlite_fts_candidates). Cast a wider net than `limit`
    # since bm25 ranking and cosine/bow scoring can disagree on the exact
    # top-k.
    fts_scores = _sqlite_fts_candidates(db, query, limit * 4)

    stmt = select(KnowledgeChunk, KnowledgeDocument.title).join(
        KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id
    )
    if fts_scores:
        stmt = stmt.where(KnowledgeChunk.id.in_(fts_scores.keys()))

    rows = db.execute(stmt).all()
    # FTS returned no hits but rows exist overall — fall back to a full scan
    # rather than reporting "nothing found" (e.g. the vector path can still
    # find a match FTS missed).
    if not rows and fts_scores is not None:
        rows = db.execute(
            select(KnowledgeChunk, KnowledgeDocument.title).join(
                KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id
            )
        ).all()
    if not rows:
        return []

    scored: list[tuple[KnowledgeChunk, str, float]] = []
    q_bow = _bow(query)

    for chunk, title in rows:
        score = 0.0
        # vector path
        if query_embedding and chunk.embedding is not None:
            emb = chunk.embedding
            if isinstance(emb, list) and emb:
                score = _vec_cosine(query_embedding, emb)
        # lexical path (always available, blends when no vector)
        if score == 0.0:
            score = _cosine(q_bow, _bow(chunk.content))
            # boost exact keyword hits
            q_lower = query.lower()
            if q_lower and q_lower in (chunk.content or "").lower():
                score += 0.25
        # FTS5 (trigram) rank — the reliable signal for Chinese content,
        # where the bow path above frequently scores 0 (see
        # _sqlite_fts_candidates). Take whichever score is higher rather
        # than overriding a genuine vector/bow match.
        if fts_scores and chunk.id in fts_scores:
            score = max(score, fts_scores[chunk.id])
        scored.append((chunk, title or "", float(score)))

    scored.sort(key=lambda x: x[2], reverse=True)
    return [s for s in scored if s[2] > 0][:limit]


def build_rag_context(hits: list[tuple[KnowledgeChunk, str, float]]) -> str:
    parts = []
    for i, (chunk, title, score) in enumerate(hits, 1):
        parts.append(f"[{i}] ({title}, score={score:.3f})\n{chunk.content}")
    return "\n\n".join(parts)


async def rag_answer(
    db: Session, question: str, limit: int = 5, live_context: Optional[str] = None
) -> tuple[str, list[str]]:
    """Retrieve relevant chunks (real embedding-model similarity if downloaded, else
    bag-of-words) and produce an answer (real local LLM first, then external API,
    else an offline extractive summary).

    `live_context`, when given, is a snapshot of real platform data assembled by
    app/services/ai_context.gather_live_context() — devices/gateways/measurements/
    work orders/assets/lab/alarms — and is merged in alongside the knowledge-base
    excerpts so answers can be grounded in live data even when no document chunk
    matches the question (e.g. "现在有哪些设备离线" needs no knowledge-base hit at all).
    """
    emb_list = await embed_texts([question], db=db)
    q_emb = emb_list[0] if emb_list else None
    hits = search_chunks(db, question, limit=limit, query_embedding=q_emb)
    sources = [f"{title}#chunk{chunk.id}" for chunk, title, _ in hits]

    kb_context = build_rag_context(hits) if hits else None
    if not kb_context and not live_context:
        return (
            "No relevant knowledge found in the knowledge base, and no platform data "
            "matched this question. Upload documents under Knowledge → Upload, then try again.",
            [],
        )

    context_parts = []
    if live_context:
        context_parts.append(f"平台实时数据：\n{live_context}")
    if kb_context:
        context_parts.append(f"知识库资料：\n{kb_context}")
    context = "\n\n".join(context_parts)

    prompt = (
        "You are an industrial analyzer O&M assistant. Answer the question using the "
        "following platform data and/or knowledge base excerpts. If neither is relevant "
        "to the question, say so plainly instead of guessing.\n\n"
        f"{context}\n\nQuestion: {question}\n\nAnswer:"
    )

    # try the real local model first
    from starlette.concurrency import run_in_threadpool
    from app.services import llm_local
    from app.services.ai_cores import prompts as _prompts

    system_rag = (_prompts() or {}).get("system_rag")
    local_answer = await run_in_threadpool(llm_local.chat_local, db, prompt, system_rag)
    if local_answer:
        return local_answer, sources

    # then an optional external LLM
    if settings.AI_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                resp = await client.post(
                    f"{settings.AI_API_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.AI_API_KEY}"},
                    json={
                        "model": settings.AI_CHAT_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                answer = resp.json()["choices"][0]["message"]["content"]
                return answer, sources
        except Exception:
            pass

    # offline extractive answer
    bullets = []
    if live_context:
        bullets.append(f"[平台数据]\n{live_context}")
    for i, (chunk, title, score) in enumerate(hits, 1):
        bullets.append(f"{i}. [{title}] {chunk.content}")
    answer = (
        "Based on the platform data and/or knowledge base (offline mode):\n\n"
        + "\n\n".join(bullets)
        + "\n\n(Configure AI_API_KEY, or download a local LLM in AI Center, for generative answers.)"
    )
    return answer, sources
