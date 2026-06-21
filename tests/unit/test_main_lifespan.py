"""Unit tests for the FastAPI application lifespan + glue in ``main``.

Every collaborator the lifespan touches (migrations, db init/close, plugin
loader, scheduler, vocab loader, session) is patched with an Async/Mock so no
real database, scheduler, filesystem, or plugins are involved. The tests assert
the startup/shutdown call *sequence*, that the scheduler branch honours
``settings.scheduler_enabled`` both ways, and the cheap wiring (`root`,
`create_app`).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from submissions_checker import main as main_module


def _patch_lifespan(monkeypatch, *, scheduler_enabled: bool, s3: bool = False):
    """Patch every lifespan collaborator. Returns (recorder, mocks-dict)."""
    recorder = MagicMock()

    settings = MagicMock(
        scheduler_enabled=scheduler_enabled,
        plugins_dir="/tmp/plugins",
        s3_endpoint_url="http://s3" if s3 else None,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    # load_vocabularies is sync; record its call via the shared recorder.
    monkeypatch.setattr(
        main_module, "load_vocabularies",
        MagicMock(side_effect=lambda *a, **k: recorder("load_vocabularies")),
    )

    run_migrations = AsyncMock(side_effect=lambda *a, **k: recorder("run_migrations"))
    init_db = AsyncMock(side_effect=lambda *a, **k: recorder("init_db"))
    close_db = AsyncMock(side_effect=lambda *a, **k: recorder("close_db"))
    monkeypatch.setattr(main_module, "run_migrations", run_migrations)
    monkeypatch.setattr(main_module, "init_db", init_db)
    monkeypatch.setattr(main_module, "close_db", close_db)

    # get_session() -> async context manager yielding a fake db.
    fake_db = AsyncMock()
    session_cm = AsyncMock()
    session_cm.__aenter__.return_value = fake_db
    session_cm.__aexit__.return_value = False
    monkeypatch.setattr(main_module, "get_session", MagicMock(return_value=session_cm))

    # PluginLoader().load_all(...) is async.
    load_all = AsyncMock(side_effect=lambda *a, **k: recorder("load_all"))
    loader_instance = MagicMock(load_all=load_all)
    monkeypatch.setattr(
        main_module, "PluginLoader", MagicMock(return_value=loader_instance)
    )

    # StorageService is only constructed when s3 endpoint is set.
    storage_ctor = MagicMock()
    monkeypatch.setattr(main_module, "StorageService", storage_ctor)

    init_scheduler = MagicMock(side_effect=lambda *a, **k: recorder("init_scheduler"))
    start_scheduler = AsyncMock(side_effect=lambda *a, **k: recorder("start_scheduler"))
    shutdown_scheduler = AsyncMock(
        side_effect=lambda *a, **k: recorder("shutdown_scheduler")
    )
    monkeypatch.setattr(main_module, "init_scheduler", init_scheduler)
    monkeypatch.setattr(main_module, "start_scheduler", start_scheduler)
    monkeypatch.setattr(main_module, "shutdown_scheduler", shutdown_scheduler)

    return recorder, {
        "settings": settings,
        "run_migrations": run_migrations,
        "init_db": init_db,
        "close_db": close_db,
        "load_all": load_all,
        "loader_instance": loader_instance,
        "storage_ctor": storage_ctor,
        "init_scheduler": init_scheduler,
        "start_scheduler": start_scheduler,
        "shutdown_scheduler": shutdown_scheduler,
        "get_session": main_module.get_session,
        "fake_db": fake_db,
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
            "load_all",
            "init_scheduler",
            "start_scheduler",
        ]

    # After exiting: shutdown ran (scheduler shutdown then close_db).
    all_calls = [c.args[0] for c in recorder.call_args_list]
    assert all_calls[-2:] == ["shutdown_scheduler", "close_db"]

    m["run_migrations"].assert_awaited_once()
    m["init_db"].assert_awaited_once()
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


async def test_lifespan_loads_plugins_with_session(monkeypatch) -> None:
    recorder, m = _patch_lifespan(monkeypatch, scheduler_enabled=False)

    async with main_module.lifespan(MagicMock()):
        pass

    # No s3 endpoint -> storage is None, StorageService never constructed.
    m["storage_ctor"].assert_not_called()
    m["loader_instance"].load_all.assert_awaited_once()
    # The session yielded by get_session() was passed as the db arg.
    _, kwargs = m["loader_instance"].load_all.call_args
    args, _ = m["loader_instance"].load_all.call_args
    assert m["fake_db"] in args or m["fake_db"] in kwargs.values()
    assert kwargs.get("storage") is None


async def test_lifespan_constructs_storage_when_s3_configured(monkeypatch) -> None:
    recorder, m = _patch_lifespan(monkeypatch, scheduler_enabled=False, s3=True)

    async with main_module.lifespan(MagicMock()):
        pass

    m["storage_ctor"].assert_called_once_with(m["settings"])
    _, kwargs = m["loader_instance"].load_all.call_args
    assert kwargs.get("storage") is m["storage_ctor"].return_value


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
