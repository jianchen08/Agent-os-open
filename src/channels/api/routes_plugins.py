"""Plugin hot-reload API routes.

Provides REST endpoints for:
- Listing plugin status (loaded, error, etc.)
- Triggering reload of specific plugins
- Reloading all plugins
- Viewing reload history

All endpoints require Bearer token authentication.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from channels.api.deps import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/plugins",
    tags=["插件热重载"],
    dependencies=[Depends(require_auth)],
)

# Global hot-reloader reference, set during app startup
_hot_reloader: Any | None = None


def set_hot_reloader(reloader: Any) -> None:
    """Set the global PluginHotReloader reference.

    Called once during application startup to make the reloader
    available to the API routes.

    Args:
        reloader: PluginHotReloader instance.
    """
    global _hot_reloader  # noqa: PLW0603
    _hot_reloader = reloader


def _get_reloader() -> Any:
    """Get the hot-reloader, raising 503 if not configured.

    Returns:
        PluginHotReloader instance.

    Raises:
        HTTPException: Hot-reloader not configured.
    """
    if _hot_reloader is None:
        raise HTTPException(
            status_code=503,
            detail="Plugin hot-reloader is not configured",
        )
    return _hot_reloader


@router.get(
    "/status",
    response_model=list[dict[str, Any]],
    summary="List plugin status",
)
def list_plugin_status() -> list[dict[str, Any]]:
    """List status information for all tracked plugins.

    Returns each plugin's config path, type, ID, status, last load time,
    last error, and version.

    When the hot-reloader is not configured (e.g. not initialized during
    app startup), returns an empty list so the page can render normally.

    Returns:
        List of plugin status dicts.
    """
    if _hot_reloader is None:
        return []
    return _hot_reloader.get_plugin_status()


@router.post(
    "/reload",
    response_model=dict[str, Any],
    summary="Reload a specific plugin",
)
def reload_plugin(
    config_path: str = Query(..., description="Config file path (relative to config/ or absolute)"),
) -> dict[str, Any]:
    """Trigger reload of a specific plugin config file.

    Args:
        config_path: Path to the YAML config file (relative to config/ or absolute).

    Returns:
        Reload result dict with success, error, and rolled_back fields.
    """
    reloader = _get_reloader()

    event = reloader.reload_plugin(config_path)
    return {
        "config_path": event.config_path,
        "config_type": event.config_type,
        "success": event.success,
        "error": event.error,
        "rolled_back": event.rolled_back,
    }


@router.post(
    "/reload-all",
    response_model=list[dict[str, Any]],
    summary="Reload all plugins",
)
def reload_all_plugins() -> list[dict[str, Any]]:
    """Reload every YAML config file under config/.

    Returns:
        List of reload result dicts.
    """
    reloader = _get_reloader()

    events = reloader.reload_all()
    return [
        {
            "config_path": e.config_path,
            "config_type": e.config_type,
            "event_type": e.event_type,
            "success": e.success,
            "error": e.error,
            "rolled_back": e.rolled_back,
        }
        for e in events
    ]


@router.get(
    "/history",
    response_model=list[dict[str, Any]],
    summary="View reload history",
)
def get_reload_history(
    limit: int = Query(default=50, ge=1, le=200, description="Max events to return"),
) -> list[dict[str, Any]]:
    """View recent plugin reload events.

    Args:
        limit: Maximum number of events to return (1-200).

    Returns:
        List of reload event dicts, most recent first.
    """
    if _hot_reloader is None:
        return []
    return _hot_reloader.get_reload_history(limit=limit)
