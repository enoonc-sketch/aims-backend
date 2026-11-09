"""Runs the real, locally-downloaded models (GGUF, via llama.cpp) — this is what makes
Chat and RAG use an actual language model / embedding model instead of canned text and
bag-of-words, once the person has clicked "Download to DB" on a model in the AI Center
> Models tab and llama-cpp-python is installed.

Everything here degrades gracefully to None when:
  - llama-cpp-python isn't installed (`pip install llama-cpp-python`), or
  - the selected model hasn't been downloaded yet.
Callers (app/api/v1/ai.py, app/services/rag.py) treat None as "fall through to the
next option" (external API, then rule-based/offline text) exactly as before.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger("aims.llm_local")

_handles: dict[str, Any] = {}
_lock = threading.Lock()

_WARNED_MISSING_BACKEND = False

# llama.cpp contexts are NOT thread-safe, and the API layer calls us through
# run_in_threadpool — two concurrent chat/RAG requests on the same handle can
# corrupt state or crash. Serialize inference per kind.
_infer_locks: dict[str, threading.Lock] = {"llm": threading.Lock(), "embedding": threading.Lock()}

_TRUNC_MARK = "\n…（内容过长，已截断中间部分）…\n"

# Records *why* the active local model can't be used right now, so the chat UI
# can show something more useful than a one-size-fits-all "offline mode"
# message. Keyed by kind ('llm' | 'embedding'); cleared on a successful load.
_last_unavailable_reason: dict[str, str] = {}


def backend_available() -> bool:
    try:
        import llama_cpp  # noqa: F401

        return True
    except Exception:
        return False


def unload(kind: str) -> None:
    """Drop the cached llama.cpp handle(s) of this kind.

    Handles are cached under "<kind>:<core_id>", so a plain pop(kind) never matched
    anything (the old model stayed resident in RAM after switching)."""
    with _lock:
        for k in list(_handles):
            if k == kind or k.startswith(f"{kind}:"):
                _handles.pop(k, None)


def _get_handle(db, kind: str):
    """kind: 'llm' | 'embedding'. Returns a loaded llama_cpp.Llama or None."""
    global _WARNED_MISSING_BACKEND

    from app.services import model_registry, ai_cores
    from app.core.config import settings

    if kind == "llm" and not settings.AI_LOCAL_LLM_ENABLED:
        _last_unavailable_reason[kind] = "本地 LLM 已在配置中禁用 (AI_LOCAL_LLM_ENABLED=false)"
        return None
    if kind == "embedding" and not settings.AI_LOCAL_EMBEDDING_ENABLED:
        _last_unavailable_reason[kind] = "本地 embedding 已在配置中禁用 (AI_LOCAL_EMBEDDING_ENABLED=false)"
        return None

    core_id = model_registry.get_active(kind)
    if not core_id:
        _last_unavailable_reason[kind] = "尚未选择要激活的模型"
        return None

    cache_key = f"{kind}:{core_id}"
    with _lock:
        cached = _handles.get(cache_key)
    if cached is not None:
        return cached

    path = ai_cores.get_model_path(db, core_id)
    if path is None:
        _last_unavailable_reason[kind] = f"模型 {core_id} 尚未下载完成，请到 模型下载 页面下载"
        return None  # selected model hasn't been downloaded yet

    try:
        from llama_cpp import Llama
    except Exception as e:
        if not _WARNED_MISSING_BACKEND:
            logger.warning(
                "llama-cpp-python is not installed (or failed to import: %s) — "
                "local models won't run. Install with: pip install llama-cpp-python",
                e,
            )
            _WARNED_MISSING_BACKEND = True
        _last_unavailable_reason[kind] = (
            f"推理引擎 llama-cpp-python 未安装或加载失败（{e.__class__.__name__}: {e}）— "
            "如果这是打包后的 EXE，通常是因为构建时漏打包了 numpy / llama_cpp 的依赖库"
        )
        return None

    try:
        if kind == "embedding":
            handle = Llama(
                model_path=str(path),
                embedding=True,
                n_ctx=512,
                n_threads=settings.AI_LOCAL_LLM_THREADS,
                n_gpu_layers=settings.AI_LOCAL_LLM_GPU_LAYERS,
                verbose=False,
            )
        else:
            handle = Llama(
                model_path=str(path),
                n_ctx=settings.AI_LOCAL_LLM_CTX,
                n_threads=settings.AI_LOCAL_LLM_THREADS,
                n_gpu_layers=settings.AI_LOCAL_LLM_GPU_LAYERS,
                verbose=False,
            )
    except Exception as e:
        logger.exception("Failed to load local model %s from %s", core_id, path)
        _last_unavailable_reason[kind] = (
            f"模型文件加载失败（{e.__class__.__name__}: {e}）— 文件可能已损坏，请重新下载"
        )
        return None

    _last_unavailable_reason.pop(kind, None)
    with _lock:
        # only keep one handle per kind loaded at a time (these are large in RAM)
        for k in list(_handles):
            if k.startswith(f"{kind}:"):
                _handles.pop(k, None)
        _handles[cache_key] = handle
    logger.info("Loaded local %s model %s from %s", kind, core_id, path)
    return handle



def _tokenize(handle, text: str) -> list[int]:
    return handle.tokenize(text.encode("utf-8"), add_bos=False, special=False)


def _detokenize(handle, tokens: list[int]) -> str:
    return handle.detokenize(tokens).decode("utf-8", errors="ignore")


def _fit_text(handle, text: str, budget: int, head_ratio: float = 0.6) -> str:
    """Trim `text` to at most `budget` tokens, keeping the beginning (instructions /
    context header) and the END (the user's question is always last in our prompts),
    dropping the middle. Returns the text unchanged if it already fits."""
    if budget <= 0:
        return ""
    try:
        toks = _tokenize(handle, text)
    except Exception:
        # tokenizer unavailable -> conservative char cap (CJK can be ~1-2 tokens/char)
        cap = max(budget, 1)
        return text if len(text) <= cap else text[: cap // 2] + _TRUNC_MARK + text[-(cap // 2):]
    if len(toks) <= budget:
        return text
    mark_len = len(_tokenize(handle, _TRUNC_MARK))
    keep = max(budget - mark_len, 2)
    head_n = int(keep * head_ratio)
    tail_n = keep - head_n
    head = _detokenize(handle, toks[:head_n])
    tail = _detokenize(handle, toks[-tail_n:]) if tail_n > 0 else ""
    return head + _TRUNC_MARK + tail


def status(db) -> dict[str, Any]:
    from app.services import model_registry

    with _lock:
        loaded_keys = set(_handles.keys())
    active_llm = model_registry.get_active("llm")
    active_embedding = model_registry.get_active("embedding")
    return {
        "active_llm": active_llm,
        "active_embedding": active_embedding,
        "llm_loaded": f"llm:{active_llm}" in loaded_keys,
        "embedding_loaded": f"embedding:{active_embedding}" in loaded_keys,
        "llm_backend_available": backend_available(),
        "llm_unavailable_reason": _last_unavailable_reason.get("llm"),
        "embedding_unavailable_reason": _last_unavailable_reason.get("embedding"),
    }


def unavailable_reason(kind: str) -> Optional[str]:
    """Human-readable reason the active local model of this kind isn't usable
    right now, populated as a side effect of the most recent _get_handle() call
    (chat_local / embed_local). None means it loaded fine last time it was tried."""
    return _last_unavailable_reason.get(kind)


def chat_local(db, prompt: str, system: Optional[str] = None) -> Optional[str]:
    """Run a chat completion on the active local LLM. Returns None if unavailable
    (not downloaded, backend missing, or inference failed) so callers can fall back.

    The prompt is fitted to the model's context window first: the live-platform
    context + RAG excerpts routinely exceed the default 4096 tokens, and llama.cpp
    raises "Requested tokens exceed context window" instead of truncating — which
    used to surface as a silent, reason-less fall-back to offline mode."""
    from app.core.config import settings

    handle = _get_handle(db, "llm")
    if handle is None:
        return None

    max_new = int(settings.AI_LOCAL_LLM_MAX_TOKENS)
    try:
        n_ctx = int(handle.n_ctx())
    except Exception:
        n_ctx = int(settings.AI_LOCAL_LLM_CTX)

    # leave room for the reply + chat-template tokens (role markers etc.)
    overhead = 64
    system_budget = max((n_ctx - max_new - overhead) // 4, 64)
    try:
        if system:
            system = _fit_text(handle, system, system_budget)
        sys_tokens = len(_tokenize(handle, system)) if system else 0
    except Exception:
        sys_tokens = 0
    prompt_budget = max(n_ctx - max_new - overhead - sys_tokens, 128)
    try:
        prompt = _fit_text(handle, prompt, prompt_budget)
    except Exception:
        logger.exception("Could not fit prompt to context window")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    def _run(msgs, max_tokens):
        with _infer_locks["llm"]:
            result = handle.create_chat_completion(
                messages=msgs,
                max_tokens=max_tokens,
                temperature=0.3,
            )
        return (result["choices"][0]["message"]["content"] or "").strip()

    try:
        answer = _run(messages, max_new)
        if not answer:
            _last_unavailable_reason["llm"] = "模型已加载但返回了空回复，请重试或换一个更大的模型"
            return None
        _last_unavailable_reason.pop("llm", None)
        return answer
    except Exception as e:
        msg = str(e)
        if "exceed context window" in msg or "context window" in msg:
            # last-resort retry with a much smaller prompt
            try:
                small = _fit_text(handle, prompt, max((n_ctx - max_new - overhead - sys_tokens) // 2, 128))
                answer = _run(
                    ([{"role": "system", "content": system}] if system else [])
                    + [{"role": "user", "content": small}],
                    max_new,
                )
                if answer:
                    _last_unavailable_reason.pop("llm", None)
                    return answer
            except Exception as e2:
                e = e2
        logger.exception("Local LLM inference failed")
        _last_unavailable_reason["llm"] = (
            f"模型已加载但推理失败（{e.__class__.__name__}: {e}）— "
            "可尝试调大 AI_LOCAL_LLM_CTX、换用更小的问题，或重新下载模型文件"
        )
        # drop the (possibly wedged) handle so the next call reloads it cleanly
        unload("llm")
        return None


def embed_local(db, texts: list[str]) -> Optional[list[list[float]]]:
    """Real sentence embeddings from the active local embedding model."""
    if not texts:
        return None
    handle = _get_handle(db, "embedding")
    if handle is None:
        return None
    try:
        try:
            n_ctx = int(handle.n_ctx())
        except Exception:
            n_ctx = 512
        # embedding context is small (512): over-long chunks make llama.cpp raise, which
        # used to silently disable semantic search. Trim each text to fit instead.
        fitted = [_fit_text(handle, t, max(n_ctx - 8, 16), head_ratio=0.8) for t in texts]
        with _infer_locks["embedding"]:
            result = handle.create_embedding(fitted)
        items = sorted(result["data"], key=lambda x: x["index"])
        _last_unavailable_reason.pop("embedding", None)
        return [it["embedding"] for it in items]
    except Exception as e:
        logger.exception("Local embedding inference failed")
        _last_unavailable_reason["embedding"] = f"embedding 推理失败（{e.__class__.__name__}: {e}）"
        return None
