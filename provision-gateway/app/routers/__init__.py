"""FastAPI route modules for provision-gateway."""

from .auth import router as auth_router
from .system import router as system_router
from .services import router as services_router
from .users import router as users_router
from .tasks import router as tasks_router
from .llm import router as llm_router
from .audit import router as audit_router

__all__ = [
    "auth_router",
    "system_router",
    "services_router",
    "users_router",
    "tasks_router",
    "llm_router",
    "audit_router",
]
