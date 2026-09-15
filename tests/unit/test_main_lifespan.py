"""Unit tests for the FastAPI application lifespan + glue in ``main``.

Every collaborator the lifespan touches (migrations, db init/close, scheduler,
vocab loader) is patched with an Async/Mock so no real database, scheduler, or
filesystem is involved. The tests assert the startup/shutdown call *sequence*,
that the scheduler branch honours ``settings.scheduler_enabled`` both ways, and
the cheap wiring (`root`, `create_app`).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from submissions_checker import main as main_module


def _patch_lifespan(monkeypatch, *, scheduler_enabled: bool):
    """Patch every lifespan collaborator. Returns (recorder, mocks-dict)."""
    recorder = MagicMock()

    settings = MagicMock(scheduler_enabled=scheduler_enabled)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    # load_vocabularies is sync; record its call via the shared recorder.
    monkeypatch.setattr(
        main_module,
        "load_vocabularies",
        MagicMock(side_effect=lambda *a, **k: recorder("load_vocabularies")),
    )

    run_migrations = AsyncMock(side_effect=lambda *a, **k: recorder("run_migrations"))
    init_db = AsyncMock(side_effect=lambda *a, **k: recorder("init_db"))
    close_db = AsyncMock(side_effect=lambda *a, **k: recorder("close_db"))
    monkeypatch.setattr(main_module, "run_migrations", run_migrations)
    monkeypatch.setattr(main_module, "init_db", init_db)
    monkeypatch.setattr(main_module, "close_db", close_db)
    refresh_metrics = AsyncMock(side_effect=lambda *a, **k: recorder("refresh_metrics"))
    monkeypatch.setattr(main_module, "refresh_metrics", refresh_metrics)

    init_scheduler = MagicMock(side_effect=lambda *a, **k: recorder("init_scheduler"))
    start_scheduler = AsyncMock(side_effect=lambda *a, **k: recorder("start_scheduler"))
    shutdown_scheduler = AsyncMock(side_effect=lambda *a, **k: recorder("shutdown_scheduler"))
    monkeypatch.setattr(main_module, "init_scheduler", init_scheduler)
    monkeypatch.setattr(main_module, "start_scheduler", start_scheduler)
    monkeypatch.setattr(main_module, "shutdown_scheduler", shutdown_scheduler)

    return recorder, {
        "settings": settings,
        "run_migrations": run_migrations,
        "init_db": init_db,
        "close_db": close_db,
        "refresh_metrics": refresh_metrics,
        "init_scheduler": init_scheduler,
        "start_scheduler": start_scheduler,
        "shutdown_scheduler": shutdown_scheduler,
    }


async def test_lifespan_startup_shutdown_sequence_scheduler_enabled(monkeypatch) -> None:
    recorder, m = _patch_lifespan(monkeypatch, scheduler_enabled=True)
    app = MagicMock()

    async with main_module.lifespan(app):
        # Inside the context: startup ran fully, shutdown not yet.
        startup_calls = [c.args[0] for c in recorder.call_args_list]
        assert startup_calls == [
            "load_vocabularies",
            "run_migrations",
            "init_db",
            "refresh_metrics",
            "init_scheduler",
            "start_scheduler",
        ]

    # After exiting: shutdown ran (scheduler shutdown then close_db).
    all_calls = [c.args[0] for c in recorder.call_args_list]
    assert all_calls[-2:] == ["shutdown_scheduler", "close_db"]

    m["run_migrations"].assert_awaited_once()
    m["init_db"].assert_awaited_once()
    m["refresh_metrics"].assert_awaited_once()
    m["close_db"].assert_awaited_once()
    m["start_scheduler"].assert_awaited_once()
    m["shutdown_scheduler"].assert_awaited_once()
    m["init_scheduler"].assert_called_once()


async def test_lifespan_scheduler_disabled_skips_scheduler(monkeypatch) -> None:
    recorder, m = _patch_lifespan(monkeypatch, scheduler_enabled=False)
    app = MagicMock()

    async with main_module.lifespan(app):
        pass

    m["init_scheduler"].assert_not_called()
    m["start_scheduler"].assert_not_awaited()
    m["shutdown_scheduler"].assert_not_awaited()
    # DB still closed on shutdown even with scheduler off.
    m["close_db"].assert_awaited_once()

    names = [c.args[0] for c in recorder.call_args_list]
    assert "init_scheduler" not in names
    assert "shutdown_scheduler" not in names
    assert names[-1] == "close_db"


async def test_root_redirects_to_login() -> None:
    response = await main_module.root()
    assert response.status_code == 302
    assert response.headers["location"] == "/auth/login"


def test_create_app_registers_routers_and_static(monkeypatch) -> None:
    app = main_module.create_app()

    # The app is configured (title set, lifespan attached by FastAPI).
    assert app.title == "Submissions Checker"
    assert app.router.lifespan_context is not None
    paths = {r.path for r in app.routes}
    # Routers + the static mount are registered (root "/" lives on the global
    # module-level app, not inside create_app, so it is not asserted here).
    assert any(p.startswith("/static") for p in paths)
    assert "/admin" in paths


def test_root_handler_registered_on_global_app() -> None:
    # The "/" redirect is wired onto the module-level ``app`` instance.
    paths = {r.path for r in main_module.app.routes}
    assert "/" in paths
