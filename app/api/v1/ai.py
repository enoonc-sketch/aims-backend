"""AI Center API — fault diagnosis, prediction, health scoring, and RAG chat.

Rule-based / statistical implementations are provided out of the box so the
service is fully functional without any external API key. If AI_API_KEY is
configured, `_call_llm()` can be wired up to call an OpenAI-compatible chat
completion endpoint for richer diagnosis/chat responses.
"""
import statistics
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_superuser, get_current_user, get_db
from app.core.config import settings
from app.models.ai import AIChatMessage, AIChatSession, AIDiagnosis, AIHealthScore, AIPrediction
from app.models.device import Device
from app.models.system import User
from app.models.measurement import Measurement
from app.services.ai_cores import health_penalties, match_diagnosis, prediction_config, prompts, list_ai_cores
from app.schemas.ai import (
    ActiveModelRequest,
    ActiveModelStatus,
    ChatRequest,
    ChatResponse,
    CorePackContent,
    CorePackOut,
    DiagnosisRequest,
    DiagnosisResult,
    HealthScoreResult,
    ModelRecommendResponse,
    PredictionPoint,
    PredictionRequest,
    PredictionResult,
    CoreDeleteResult,
    PyPackageInstallResult,
    PyPackageOut,
)
from app.services import py_packages, studio_data

router = APIRouter(prefix="/ai", tags=["AI Center"])


def _recent_values(db: Session, device_id: int, component_name: str, hours: int = 24) -> list[float]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = db.scalars(
        select(Measurement)
        .where(
            Measurement.device_id == device_id,
            Measurement.component_name == component_name,
            Measurement.time >= since,
            # bad 质量的读数不是证据: 不参与诊断 / 预测 / 健康评分 / 异常模型训练
            Measurement.quality != "bad",
        )
        .order_by(Measurement.time.asc())
    ).all()
    return [r.value for r in rows]


async def _call_llm(prompt: str, db: Optional[Session] = None, system: Optional[str] = None) -> Optional[str]:
    """Real local LLM first (if a GGUF model has been downloaded + selected in the
    Models tab), then an optional external OpenAI-compatible chat completion API."""
    if db is not None:
        from starlette.concurrency import run_in_threadpool
        from app.services import llm_local

        local_answer = await run_in_threadpool(llm_local.chat_local, db, prompt, system)
        if local_answer:
            return local_answer

    if not settings.AI_API_KEY:
        return None
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.AI_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {settings.AI_API_KEY}"},
            json={
                "model": settings.AI_CHAT_MODEL,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


@router.post("/diagnose", response_model=DiagnosisResult)
async def diagnose(payload: DiagnosisRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """手动诊断(可选 LLM 润色)。带 alarm_id 时绑定到该报警并刷新其未完成工单。
    规则包 / 统计 / 异常模型 / SOP / 历史复盘校准 与报警自动诊断共用 services/diagnosis_flow.py。"""
    from app.models.alarm import Alarm
    from app.services import diagnosis_flow
    from app.services.alarms_hooks import refresh_open_work_order

    device = db.get(Device, payload.device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    alarm = None
    if payload.alarm_id is not None:
        alarm = db.get(Alarm, payload.alarm_id)
        if alarm is None:
            raise HTTPException(status_code=404, detail="Alarm not found")
        if alarm.device_id != payload.device_id:
            raise HTTPException(status_code=422, detail="alarm_id does not belong to device_id")

    # kb_hits=[]: 知识库改用下面的异步检索(有本地向量模型时走语义检索), 这里不重复做同步关键词检索
    draft = diagnosis_flow.build_draft(
        db, device, payload.component_name, symptom=payload.symptom, alarm=alarm, kb_hits=[]
    )
    symptom = draft["symptom"]

    # LLM enrichment: real local model if downloaded, else external API if configured
    llm = await _call_llm(
        f"Refine this analyzer diagnosis briefly.\n"
        f"Device={payload.device_id}, component={payload.component_name}, symptom={symptom}.\n"
        f"Rule pack says: {draft['diagnosis']}\nRecommendation: {draft['recommendation']}",
        db=db,
        system="You are an industrial analyzer fault-diagnosis assistant. Be concise and concrete.",
    )
    if llm:
        draft["diagnosis"] = llm
        draft["confidence"] = min(0.95, draft["confidence"] + 0.1)

    # Bind the diagnosis to the knowledge base: surface the excerpts that speak to this fault.
    draft["evidence"]["knowledge"] = await studio_data.kb_search(db, f"{payload.component_name} {symptom} analyzer", 3)

    row = diagnosis_flow.persist(
        db, draft, alarm=alarm, source="alarm_manual" if alarm else "manual", user_id=user.id
    )
    if alarm is not None:
        refresh_open_work_order(db, alarm, row)
    db.commit()
    db.refresh(row)
    return DiagnosisResult(
        id=row.id,
        alarm_id=row.alarm_id,
        device_id=row.device_id,
        component_name=row.component_name or payload.component_name,
        symptom=row.symptom or symptom,
        symptom_auto_detected=draft["symptom_auto_detected"],
        diagnosis=row.diagnosis or draft["diagnosis"],
        confidence=float(row.confidence or draft["confidence"]),
        recommendation=row.recommendation or draft["recommendation"],
        created_at=row.created_at,
        references=[
            {"document_id": h["document_id"], "document_title": h["document_title"], "chunk_id": h["chunk_id"],
             "score": h["score"], "excerpt": h["excerpt"]}
            for h in draft["evidence"]["knowledge"]
        ],
    )


@router.post("/predict", response_model=PredictionResult)
def predict(payload: PredictionRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not db.get(Device, payload.device_id):
        raise HTTPException(status_code=404, detail="Device not found")

    # look back further than before (7 days) so the real model has enough history to fit a trend
    values = _recent_values(db, payload.device_id, payload.component_name, hours=24 * 7)
    now = datetime.now(timezone.utc)

    from app.services.forecasting import forecast

    preds, model_name, meta = forecast(values, payload.horizon_hours)
    lowers = meta.get("lower") or [None] * len(preds)
    uppers = meta.get("upper") or [None] * len(preds)

    points = [
        PredictionPoint(
            time=now + timedelta(hours=h),
            value=preds[h - 1],
            lower=lowers[h - 1],
            upper=uppers[h - 1],
        )
        for h in range(1, payload.horizon_hours + 1)
    ]

    record = AIPrediction(
        device_id=payload.device_id,
        component_name=payload.component_name,
        horizon_hours=payload.horizon_hours,
        predicted_values=[
            {"time": p.time.isoformat(), "value": p.value, "lower": p.lower, "upper": p.upper} for p in points
        ],
        model_name=model_name,
    )
    db.add(record)
    db.commit()

    return PredictionResult(
        device_id=payload.device_id,
        component_name=payload.component_name,
        horizon_hours=payload.horizon_hours,
        model_name=model_name,
        trend=meta.get("trend"),
        predicted_values=points,
    )


@router.post("/diagnosis/train")
def train_diagnosis_model(
    device_id: int,
    component_name: str,
    db: Session = Depends(get_db),
    days: int = 30,
    user: User = Depends(get_current_user),
):
    """Fit a real (unsupervised) anomaly-detection model on this device+component's
    own measurement history — no labeled fault data required. Needs at least
    anomaly.MIN_TRAINING_POINTS readings in the lookback window."""
    if not db.get(Device, device_id):
        raise HTTPException(status_code=404, detail="Device not found")

    from app.services import anomaly

    values = _recent_values(db, device_id, component_name, hours=24 * days)
    result = anomaly.train(device_id, component_name, values)
    if result is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Not enough history to train ({len(values)} points, need "
                f"{anomaly.MIN_TRAINING_POINTS}+). Try a longer `days` window or wait for more data."
            ),
        )
    return {"device_id": device_id, "component_name": component_name, **result}


@router.get("/health/{device_id}", response_model=HealthScoreResult)
def health_score(device_id: int, db: Session = Depends(get_db)):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    from app.services.ai_cores import health_penalties
    from app.models.alarm import Alarm
    from sqlalchemy import func, select

    pack = health_penalties()
    penalties = pack.get("penalties") or {}
    score = float(pack.get("base_score", 100))
    factors = {"base_health_score": float(device.health_score)}

    # status penalty
    st = (device.status or "").lower()
    key = f"device_status_{st}"
    if key in penalties:
        score -= float(penalties[key])
        factors["status"] = st
        factors["status_penalty"] = float(penalties[key])

    # alarm penalties
    active = db.scalars(select(Alarm).where(Alarm.device_id == device_id, Alarm.status == "active")).all()
    warn_n = sum(1 for a in active if (a.severity or "").lower() == "warning")
    crit_n = sum(1 for a in active if (a.severity or "").lower() == "critical")
    wpen = float(penalties.get("active_alarm_warning", 5)) * warn_n
    cpen = float(penalties.get("active_alarm_critical", 15)) * crit_n
    score -= wpen + cpen
    factors["alarms_warning"] = warn_n
    factors["alarms_critical"] = crit_n
    factors["alarm_penalty"] = wpen + cpen

    # data availability penalty: an analyzer that has stopped reporting recently is
    # itself a health signal, even before any alarm fires on it.
    since24 = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_count = (
        db.scalar(
            select(func.count())
            .select_from(Measurement)
            .where(Measurement.device_id == device_id, Measurement.time >= since24)
        )
        or 0
    )
    min_expected = int(pack.get("min_expected_points_24h", 4))
    factors["recent_measurement_count_24h"] = recent_count
    if recent_count < min_expected:
        dpen = float(penalties.get("low_data_availability", 8))
        score -= dpen
        factors["data_availability_penalty"] = dpen

    # trained anomaly-model penalty: for every component with a model already trained
    # (see /ai/diagnosis/train), fold in how anomalous its recent readings look —
    # this is the same IsolationForest model /ai/diagnose uses, applied device-wide.
    from app.services import anomaly

    components = db.scalars(
        select(Measurement.component_name)
        .where(Measurement.device_id == device_id, Measurement.time >= since24)
        .distinct()
    ).all()
    anomaly_penalty = 0.0
    anomalous_components: list[str] = []
    for comp in components[:25]:  # cap: health score should stay fast even on wide devices
        if not anomaly.is_trained(device_id, comp):
            continue
        comp_values = _recent_values(db, device_id, comp, hours=24)
        result = anomaly.score(device_id, comp, comp_values) if comp_values else None
        if result and result["is_anomaly"]:
            anomaly_penalty += float(penalties.get("trained_anomaly_detected", 12)) * result["anomaly_ratio"]
            anomalous_components.append(comp)
    if anomaly_penalty:
        score -= anomaly_penalty
        factors["anomaly_penalty"] = round(anomaly_penalty, 2)
        factors["anomalous_components"] = anomalous_components

    score = max(0.0, min(100.0, score))
    # blend with stored device health
    score = round(0.5 * score + 0.5 * float(device.health_score), 2)

    row = AIHealthScore(device_id=device_id, score=score, factors=factors)
    db.add(row)
    db.commit()
    return HealthScoreResult(device_id=device_id, score=score, factors=factors)


@router.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    session = db.get(AIChatSession, payload.session_id) if payload.session_id else None
    if not session:
        session = AIChatSession(title=payload.message[:50])
        db.add(session)
        db.commit()
        db.refresh(session)

    db.add(AIChatMessage(session_id=session.id, role="user", content=payload.message))

    # Deep binding: always pull a live snapshot of the relevant platform data
    # (devices/communication/data analysis/knowledge/maintenance/assets/lab —
    # see app/services/ai_context.py) so the assistant can read and analyze
    # real content, not just chat generically. This runs regardless of
    # use_rag, which only controls whether knowledge-base documents are ALSO
    # searched and merged in.
    from app.services.ai_context import gather_live_context

    live_context = gather_live_context(db, payload.message, device_id=payload.device_id)

    sources: list[str] = []
    if payload.use_rag:
        from app.services.rag import rag_answer
        answer, sources = await rag_answer(db, payload.message, live_context=live_context)
    else:
        from app.services.ai_cores import prompts as _prompts
        _p = _prompts()
        prompt = (
            "以下是平台当前的实时数据，回答问题时请参考（如与问题无关可忽略）：\n\n"
            f"{live_context}\n\n用户问题：{payload.message}"
        )
        llm_answer = await _call_llm(prompt, db=db, system=_p.get("system_chat"))
        if llm_answer:
            answer = llm_answer
        else:
            from app.services import llm_local

            reason = llm_local.unavailable_reason("llm")
            answer = (_p.get("offline_preamble") or "Offline AI-core mode.") + (
                f" ({reason})" if reason else ""
            ) + (
                " Download a local LLM in AI Center → 模型下载, set use_rag=true to use the"
                " knowledge base, or configure AI_API_KEY for an external LLM."
            )
            if live_context:
                answer += "\n\n以下是与你的问题相关的平台实时数据：\n" + live_context

    db.add(
        AIChatMessage(
            session_id=session.id,
            role="assistant",
            content=answer,
            sources=sources or None,
        )
    )
    db.commit()

    return ChatResponse(session_id=session.id, answer=answer, reply=answer, sources=sources)


@router.get("/cores-info")
def ai_cores_info():
    """List open-source AI cores (diagnosis rules, health weights, prompts, prediction)."""
    from app.services.ai_cores import list_ai_cores, load_json_core
    cores = list_ai_cores()
    return {
        "available": any(c.get("available") for c in cores),
        "cores": cores,
        "diagnosis_pack": (load_json_core("diagnosis_rules.json") or {}).get("name"),
        "health_pack": (load_json_core("health_weights.json") or {}).get("name"),
        "prompt_pack": (load_json_core("rag_prompts.json") or {}).get("name"),
        "prediction_pack": (load_json_core("prediction_baseline.json") or {}).get("name"),
    }


@router.get("/cores", response_model=list[CorePackOut])
def list_cores(db: Session = Depends(get_db)):
    """All downloadable cores/models (manifest) merged with what's already stored
    in the database — powers the AI Center > Models tab in the frontend."""
    from app.services.ai_cores import list_core_packs_from_db

    return list_core_packs_from_db(db)


@router.post("/cores/{core_id}/download", response_model=CorePackOut)
def download_core(core_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Download one core/model (or read its bundled local copy) and store it in the
    database — this is the "把模型下载到数据库里" action triggered from the frontend."""
    from app.services.ai_cores import download_core_to_db

    try:
        return download_core_to_db(db, core_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown core id: {core_id}")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Download failed: {e}")


@router.post("/cores/download-all", response_model=list[CorePackOut])
def download_all_cores(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Download every core listed in the manifest into the database."""
    from app.services.ai_cores import download_core_to_db, load_manifest

    results = []
    for entry in load_manifest():
        try:
            results.append(download_core_to_db(db, entry["id"]))
        except Exception as e:
            results.append({"id": entry["id"], "name": entry.get("name"), "in_db": False, "error": str(e)})
    return results


@router.get("/engine")
def engine_info():
    """AI 推理引擎信息卡（总方案 7.1）：名称/版本/安装路径/架构/运行参数/GPU 探测。

    所有字段真实采集；不支持的指标（如无 GPU 时）明确返回 null，不伪造数值。
    """
    import platform as _platform
    from pathlib import Path

    from app.core.config import settings
    from app.services.system_info import get_system_info

    engine_version = None
    engine_path = None
    try:
        import llama_cpp
        engine_version = getattr(llama_cpp, "__version__", None)
        engine_path = str(Path(llama_cpp.__file__).parent)
        installed = True
    except Exception:
        installed = False

    hw = get_system_info()
    return {
        "engine": "llama.cpp (llama-cpp-python)",
        "version": engine_version,
        "path": engine_path,
        "arch": _platform.machine(),
        "installed": installed,
        "status": llm_local_status_snapshot(),
        "params": {
            "ctx": settings.AI_LOCAL_LLM_CTX,
            "threads": settings.AI_LOCAL_LLM_THREADS,
            "gpu_layers": settings.AI_LOCAL_LLM_GPU_LAYERS,
            "max_tokens": settings.AI_LOCAL_LLM_MAX_TOKENS,
        },
        "hardware": {
            "cpu_cores": hw.get("cpu_cores"),
            "total_ram_gb": hw.get("total_ram_gb"),
            "available_ram_gb": hw.get("available_ram_gb"),
            "gpu_detected": hw.get("gpu_detected"),
            "gpu_source": hw.get("gpu_source"),
        },
    }


def llm_local_status_snapshot():
    from app.services import llm_local
    from app.api.deps import get_db as _get_db

    # 端点已有 db 依赖时用参数更严谨；这里快照用独立会话，避免签名扩散
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        return llm_local.status(db)
    finally:
        db.close()


@router.get("/models", response_model=ActiveModelStatus)
def models_status(db: Session = Depends(get_db)):
    """Which local (real, downloaded) LLM / embedding model is active and loaded.
    Powers the runtime model-switcher in AI Center → 模型下载."""
    from app.services import llm_local

    return llm_local.status(db)


@router.get("/models/recommend", response_model=ModelRecommendResponse)
def models_recommend(
    prefer: str = Query("small", pattern="^(small|quality)$"),
    lang: str = Query("zh", pattern="^(zh|en)$"),
    max_params_b: float = Query(4.0, gt=0, le=70),
    diversify: bool = Query(True),
    overseas_first: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Real-time hardware-based model recommendation for AI Center → 模型下载:
    reads this machine's current RAM/CPU/GPU (not cached) and, for every LLM /
    embedding model in the manifest, says whether it fits comfortably, is tight,
    or is unrealistic — plus a single top pick per kind. Re-evaluates on every
    call so it reflects free RAM right now, not just a one-time guess."""
    from app.services.model_recommend import recommend_models

    return recommend_models(db, prefer=prefer, lang=lang, max_small_params_b=max_params_b, diversify=diversify, overseas_first=overseas_first)


@router.post("/models/active", response_model=ActiveModelStatus)
def set_active_model(payload: ActiveModelRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Switch the active local LLM or embedding model at runtime (no restart needed).
    The chosen model must already be downloaded (see /ai/cores/{id}/download)."""
    from app.services import model_registry, ai_cores

    if payload.kind not in ("llm", "embedding"):
        raise HTTPException(status_code=400, detail="kind must be 'llm' or 'embedding'")

    entry = ai_cores.get_manifest_entry(payload.core_id)
    if entry is None or entry.get("kind") != payload.kind:
        raise HTTPException(status_code=404, detail=f"No {payload.kind} model with id {payload.core_id}")

    if ai_cores.get_model_path(db, payload.core_id) is None:
        raise HTTPException(
            status_code=409,
            detail=f"{payload.core_id} has not been downloaded yet — download it first.",
        )

    model_registry.set_active(payload.kind, payload.core_id)

    from app.services import llm_local

    return llm_local.status(db)


@router.post("/cores/{core_id}/unload", response_model=ActiveModelStatus)
def unload_model(core_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """卸载模型：释放 llama.cpp 在 RAM/VRAM 中的句柄，但保留模型文件（总方案 7.3）。

    与删除严格区分：卸载是运行时资源操作，随时可重新加载；删除才会动文件。
    """
    from app.services import ai_cores

    try:
        out = ai_cores.unload_model(db, core_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown core id: {core_id}")
    except ai_cores.ModelDeleteError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return out["status"]


@router.delete("/cores/{core_id}", response_model=CoreDeleteResult)
def delete_model(
    core_id: str,
    confirm: bool = Query(False, description="必须显式传 confirm=true 才执行删除"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_superuser),
):
    """删除模型文件（超管专用）。与卸载严格区分（总方案 7.3 / 7.4）：

    - 仅 llm/embedding 二进制模型可删（内置规则包与业务绑定，不可删）；
    - 激活中的模型拒绝删除（409，须先切换）；
    - 文件路径受控：只允许删除模型目录内的 .gguf/.bin/.onnx；
    - 删除前自动卸载推理句柄；数据库行复位为"未下载"，manifest 中仍可见可重新下载。
    """
    from app.services import ai_cores

    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="删除模型是破坏性操作：请带 ?confirm=true 重新请求（模型文件将被移除，可重新下载）",
        )
    try:
        out = ai_cores.delete_model(db, core_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown core id: {core_id}")
    except ai_cores.ModelDeleteError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return CoreDeleteResult(**{k: v for k, v in out.items() if k != "status"})


@router.post("/cores/scan-local")
def scan_local_models(db: Session = Depends(get_db), user: User = Depends(get_current_superuser)):
    """扫描模型目录中未登记的 GGUF 文件并登记（总方案 7.2 模型导入）。

    桌面部署的常规导入方式：把 .gguf 文件放进模型目录后点一次扫描。
    登记 SHA-256 与来源 local-import；路径受控，只扫描模型目录本身。

    权限：与模型加载 / 卸载 / 删除一致，属于管理员操作——它会写入模型注册表，
    普通用户不应能改变本机可用的模型集合。
    """
    from app.services import ai_cores

    return ai_cores.scan_local_models(db)


@router.get("/cores/{core_id}/content", response_model=CorePackContent)
def core_content(core_id: str, db: Session = Depends(get_db)):
    """Return a downloaded core's content straight from the database."""
    from app.services.ai_cores import get_core_pack_content

    content = get_core_pack_content(db, core_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Core not downloaded to database yet")
    return content


# ============================================================
# Runtime local-AI package installer (AI Center -> Models tab)
#
# Complements /ai/cores (which downloads GGUF *model files* into the
# database): this installs the *Python packages* that actually run
# them — llama-cpp-python (CPU/CUDA variants), numpy, scikit-learn,
# statsmodels — from inside the running app, one click, instead of only
# ever being configurable by editing requirements-exe-ai.txt and
# re-running build_exe.bat / build_onefile.bat.
# ============================================================
@router.get("/packages", response_model=list[PyPackageOut])
def list_py_packages(user: User = Depends(get_current_user)):
    # 审计项 L-5：以前完全无认证，泄露后端已装包清单（M-2 的前置侦察面）。
    return py_packages.list_packages()


@router.post("/packages/{package_id}/install", response_model=PyPackageInstallResult)
def install_py_package(package_id: str, user: User = Depends(get_current_superuser)):
    # 审计项 M-2：服务器端 pip 安装属于高危管理操作，收敛到超管。
    # 包名仍为代码内固定白名单（无任意包名注入面）；EXE 交付版直接禁用
    # （离线环境装不上，也避免供应链面）。
    if getattr(sys, "frozen", False):
        raise HTTPException(
            status_code=400,
            detail="离线安装版不支持在线安装 AI 依赖包。请使用对应版本的完整安装包。",
        )
    try:
        return py_packages.install_package(package_id)
    except py_packages.PackageInstallError as e:
        raise HTTPException(status_code=400, detail=str(e))
