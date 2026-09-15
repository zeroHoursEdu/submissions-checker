"""Health and build-identity endpoints."""

import os

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import text

from submissions_checker.api.dependencies import DBSession
from submissions_checker.core import metrics
from submissions_checker.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["health"])

# Baked into the production image at build time (see docker/app/Dockerfile) and
# deliberately not a setting: it must describe the code in the image, so it must
# not be overridable by the host's environment file.
APP_REVISION = os.environ.get("APP_REVISION") or "unknown"


@router.get("/health")
async def health_check() -> dict[str, str]:
    """
    Basic health check endpoint.

    Returns:
        Simple status indicating the service is running
    """
    return {"status": "healthy"}


@router.get("/version")
async def version() -> dict[str, str]:
    """Report the commit this image was built from.

    Deployment here is pull-initiated: CI publishes an image and the host's
    updater decides when to take it. This endpoint is how anything outside the
    host can tell whether a published build actually arrived, which is what the
    release workflow polls before calling a deployment successful.

    Unauthenticated on purpose — it reveals only a commit hash of a public
    repository, and it has to be readable by CI, which holds no host credentials.
    """
    return {"revision": APP_REVISION}


@router.get("/health/ready")
async def readiness_check(db: DBSession) -> dict[str, str]:
    """
    Readiness check with database connectivity test.

    Args:
        db: Database session

    Returns:
        Status with database connectivity information

    Raises:
        HTTPException: If database is not accessible
    """
    try:
        # Test database connectivity
        result = await db.execute(text("SELECT 1"))
        result.scalar()

        logger.info("readiness_check_passed")
        return {"status": "ready", "database": "connected"}

    except Exception as e:
        logger.error("readiness_check_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection failed",
        ) from e


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Prometheus exposition, scraped by Alloy on the compose network.

    Caddy answers 404 for this path publicly. It touches no database on purpose: it must
    stay up when Postgres is down, which is exactly when app_db_healthy=0 is worth reading.
    """
    return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)
