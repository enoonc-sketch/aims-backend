"""llm_local: prompt fitting / failure-reason / unload — uses a fake llama handle,
so it needs neither llama-cpp-python nor a downloaded model."""
import sys
import types

import pytest

from app.services import llm_local


class FakeLlama:
    """1 char == 1 token; raises like llama.cpp when the context is exceeded."""

    def __init__(self, n_ctx=200):
        self._n_ctx = n_ctx
        self.calls = []

    def n_ctx(self):
        return self._n_ctx

    def tokenize(self, b, add_bos=False, special=False):
        return list(b.decode("utf-8"))

    def detokenize(self, toks):
        return "".join(toks).encode("utf-8")

    def create_chat_completion(self, messages, max_tokens, temperature):
        total = sum(len(m["content"]) for m in messages) + max_tokens
        self.calls.append(messages)
        if total > self._n_ctx:
            raise ValueError(f"Requested tokens ({total}) exceed context window of {self._n_ctx}")
        return {"choices": [{"message": {"content": " ok "}}]}


@pytest.fixture()
def fake(monkeypatch):
    h = FakeLlama()
    llm_local._last_unavailable_reason.clear()
    monkeypatch.setattr(llm_local, "_get_handle", lambda db, kind: h)
    from app.core.config import settings
    monkeypatch.setattr(settings, "AI_LOCAL_LLM_MAX_TOKENS", 40)
    return h


def test_long_prompt_is_trimmed_not_failed(fake):
    prompt = "HEAD" + "x" * 5000 + "问题：现在几点?"
    assert llm_local.chat_local(None, prompt, "sys") == "ok"
    sent = fake.calls[-1][-1]["content"]
    assert sent.startswith("HEAD") and sent.endswith("问题：现在几点?")
    assert "已截断" in sent
    assert llm_local.unavailable_reason("llm") is None


def test_short_prompt_untouched(fake):
    assert llm_local.chat_local(None, "hi") == "ok"
    assert fake.calls[-1][-1]["content"] == "hi"


def test_inference_failure_records_reason_and_unloads(monkeypatch, fake):
    def boom(**kw):
        raise RuntimeError("llama_decode returned -1")

    monkeypatch.setattr(fake, "create_chat_completion", lambda messages, max_tokens, temperature: boom())
    llm_local._handles["llm:x"] = object()
    assert llm_local.chat_local(None, "hi") is None
    assert "推理失败" in llm_local.unavailable_reason("llm")
    assert "llm:x" not in llm_local._handles


def test_unload_matches_kind_prefix():
    llm_local._handles.clear()
    llm_local._handles["llm:a"] = object()
    llm_local._handles["embedding:b"] = object()
    llm_local.unload("llm")
    assert list(llm_local._handles) == ["embedding:b"]
    llm_local._handles.clear()
