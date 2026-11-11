from fastapi import APIRouter

from app.api.v1 import (
    ai,
    ai_devices,
    ai_feedback,
    import_validation,
    templates,
    industrial,
    ai_studio,
    alarms,
    analysis,
    assets,
    auth,
    communication,
    dashboard,
    devices,
    external_data,
    gateways,
    knowledge,
    lab,
    maintenance,
    measurements,
    system,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(dashboard.router)
api_router.include_router(devices.router)
api_router.include_router(gateways.router)
api_router.include_router(communication.router)
api_router.include_router(measurements.router)
api_router.include_router(alarms.router)
api_router.include_router(ai.router)
api_router.include_router(ai_devices.router)
api_router.include_router(ai_feedback.router)
api_router.include_router(ai_studio.router)
api_router.include_router(ai_studio.lab_router)
api_router.include_router(maintenance.router)
api_router.include_router(assets.router)
api_router.include_router(analysis.router)
api_router.include_router(lab.router)
api_router.include_router(knowledge.router)
api_router.include_router(external_data.router)
api_router.include_router(system.router)
api_router.include_router(industrial.router)
api_router.include_router(import_validation.router)
api_router.include_router(templates.router)
