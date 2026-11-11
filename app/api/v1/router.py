from fastapi import APIRouter, Depends

from app.api.deps import get_current_user, get_current_superuser
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

# Security hardening (AIMS 1.0 phase 1, report item H-1): every business router
# now requires an authenticated caller. Previously ~70 GET endpoints were
# reachable anonymously and could leak device inventories, measurement history,
# alarm data, knowledge-base text, external-vendor account metadata and AI
# diagnosis history once the backend was exposed beyond 127.0.0.1.
#
# - `_auth`  : any logged-in user
# - `_admin` : superuser only — the external-data routers expose vendor
#              platform account names / login URLs / CSS selectors, so they
#              are confined to administrators (report items M-6/M-1).
# The auth router itself stays public (login/refresh must be reachable without
# a token); system.py keeps its per-endpoint superuser checks.
_auth = [Depends(get_current_user)]
_admin = [Depends(get_current_superuser)]

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(dashboard.router, dependencies=_auth)
api_router.include_router(devices.router, dependencies=_auth)
api_router.include_router(gateways.router, dependencies=_auth)
api_router.include_router(communication.router, dependencies=_auth)
api_router.include_router(measurements.router, dependencies=_auth)
api_router.include_router(alarms.router, dependencies=_auth)
api_router.include_router(ai.router, dependencies=_auth)
api_router.include_router(ai_devices.router, dependencies=_auth)
api_router.include_router(ai_feedback.router, dependencies=_auth)
api_router.include_router(ai_studio.router, dependencies=_auth)
api_router.include_router(ai_studio.lab_router, dependencies=_auth)
api_router.include_router(maintenance.router, dependencies=_auth)
api_router.include_router(assets.router, dependencies=_auth)
api_router.include_router(analysis.router, dependencies=_auth)
api_router.include_router(lab.router, dependencies=_auth)
api_router.include_router(knowledge.router, dependencies=_auth)
api_router.include_router(external_data.router, dependencies=_admin)
api_router.include_router(system.router, dependencies=_auth)
api_router.include_router(industrial.router, dependencies=_auth)
api_router.include_router(import_validation.router, dependencies=_auth)
api_router.include_router(templates.router, dependencies=_auth)
