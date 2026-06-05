"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from pathlib import Path

from submissions_checker.api.routes import admin, analytics, auth, feedback, health, i18n, notifications, student_portal, student_quiz, teacher_portal, users, webhooks
from submissions_checker.core.config import get_settings
from submissions_checker.core.database import close_db, init_db
from submissions_checker.core.i18n import load_vocabularies
from submissions_checker.core.logging import configure_logging, get_logger
from submissions_checker.core.migrations import run_migrations
from submissions_checker.core.scheduler import (
    init_scheduler,
    shutdown_scheduler,
    start_scheduler,
)
from submissions_checker.db.session import get_session
from submissions_checker.services.plugin_loader import PluginLoader
from submissions_checker.services.storage import StorageService

# Configure logging before anything else
configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan context manager.

    Handles startup and shutdown events:
    - Startup: Run migrations, initialize database, start scheduler
    - Shutdown: Stop scheduler, close database connections
    """
    settings = get_settings()

    # Startup
    logger.info("application_starting")

    # 0. Load i18n vocabularies
    load_vocabularies(Path("i18n"))
    logger.info("i18n_loaded")

    # 1. Run database migrations
    logger.info("running_database_migrations")
    await run_migrations()
    logger.info("migrations_completed")

    # 2. Initialize database connection pool
    await init_db()
    logger.info("database_initialized")

    # 3. Load subject plugins from plugins directory
    plugins_dir = Path(settings.plugins_dir)
    storage = StorageService(settings) if settings.s3_endpoint_url else None
    async with get_session() as db:
        await PluginLoader().load_all(plugins_dir, db, storage=storage)
    logger.info("plugins_loaded", plugins_dir=str(plugins_dir))

    # 4. Start scheduler (if enabled)
    if settings.scheduler_enabled:
        init_scheduler()
        await start_scheduler()
    else:
        logger.info("scheduler_disabled")

    logger.info("application_started")

    yield

    # Shutdown
    logger.info("application_shutting_down")

    # 1. Shutdown scheduler
    if settings.scheduler_enabled:
        await shutdown_scheduler()

    # 2. Close database connections
    await close_db()

    logger.info("application_shutdown_complete")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="Submissions Checker",
        description="Automated student code submission checker with GitHub integration",
        version="0.1.0",
        lifespan=lifespan,
        debug=settings.debug,
    )

    # Configure CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routers
    app.include_router(health.router)
    app.include_router(feedback.router)
    app.include_router(webhooks.router)
    app.include_router(users.router)
    app.include_router(auth.router)
    app.include_router(student_portal.router)
    app.include_router(student_quiz.router)
    app.include_router(teacher_portal.router)
    app.include_router(analytics.router)
    app.include_router(admin.router)
    app.include_router(notifications.router)
    app.include_router(i18n.router)

    # Serve static assets
    app.mount("/static", StaticFiles(directory="static"), name="static")

    logger.info(
        "application_configured",
        environment=settings.environment,
        debug=settings.debug,
    )

    return app


# Create application instance
app = create_app()


@app.get("/")
async def root() -> RedirectResponse:
    """Redirect to login."""
    return RedirectResponse(url="/auth/login", status_code=302)
