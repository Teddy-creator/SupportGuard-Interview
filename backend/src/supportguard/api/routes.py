from __future__ import annotations

from fastapi import APIRouter

from supportguard.api.endpoints.actions import router as actions_router
from supportguard.api.endpoints.approvals import router as approvals_router
from supportguard.api.endpoints.conversations import router as conversations_router
from supportguard.api.endpoints.events import router as events_router
from supportguard.api.endpoints.sessions import router as sessions_router
from supportguard.api.messages import router as messages_router

router = APIRouter()
router.include_router(sessions_router)
router.include_router(conversations_router)
router.include_router(messages_router)
router.include_router(actions_router)
router.include_router(events_router)
router.include_router(approvals_router)
