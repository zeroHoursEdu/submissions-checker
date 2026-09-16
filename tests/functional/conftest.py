"""Foundation fixtures for functional API tests.

Spins up a single PostgreSQL container for the whole test session, creates the
full schema from the SQLAlchemy metadata (every model, not just a subset), and
drives the real FastAPI ``app`` over ASGI with the ``get_db`` dependency
overridden to the test database. The full auth/authz stack is active, so these
tests are the primary coverage for permissions and security.

Each test runs against a freshly-truncated database (``RESTART IDENTITY``), so
ids are deterministic and tests are isolated and order-independent.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

# Importing the models package registers every table on ``Base.metadata`` so
# ``create_all`` builds the complete schema (db.base alone only registers a subset).
import submissions_checker.db.models  # noqa: F401
from submissions_checker.core import database as database_module
from submissions_checker.core.security import COOKIE_NAME, create_access_token, hash_password
from submissions_checker.db.models.base import Base
from submissions_checker.db.models.enums import UserRole
from submissions_checker.db.models.group import Group
from submissions_checker.db.models.student import Student
from submissions_checker.db.models.user import User
from submissions_checker.main import app

# ── Infrastructure ────────────────────────────────────────────────────────────


@pytest.fixture(scope="session", autouse=True)
def _load_i18n() -> None:
    """Load translation vocabularies once.

    The app normally does this in its lifespan handler, which ASGITransport does
    not trigger, so HTML routes that reference ``vocab.*`` would otherwise fail.
    """
    from pathlib import Path

    from submissions_checker.core.i18n import load_vocabularies

    load_vocabularies(Path("i18n"))


@pytest.fixture(scope="session")
def functional_pg() -> PostgresContainer:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def _schema_ready(functional_pg: PostgresContainer) -> bool:
    """Create the full schema exactly once using a synchronous engine.

    DDL is run on a sync (psycopg2) engine so it never touches an asyncio event
    loop — that keeps the async engine free to be function-scoped (bound to each
    test's own loop), which is what pytest-asyncio requires.
    """
    from sqlalchemy import create_engine

    sync_engine = create_engine(functional_pg.get_connection_url())
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    return True


@pytest.fixture
async def functional_engine(functional_pg: PostgresContainer, _schema_ready: bool):
    """Function-scoped async engine — created and disposed on the test's own loop."""
    engine = create_async_engine(functional_pg.get_connection_url(driver="asyncpg"))
    yield engine
    await engine.dispose()


@pytest.fixture
def functional_sessionmaker(
    functional_engine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(functional_engine, expire_on_commit=False, autoflush=False)


@pytest.fixture(autouse=True)
async def _clean_database(
    functional_engine,
) -> AsyncGenerator[None, None]:
    """Truncate every table (and reset identities) before each test for isolation."""
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with functional_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture(autouse=True)
def _override_get_db(
    functional_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None, None]:
    """Point the app's ``get_db`` dependency at the test database."""

    async def _get_db_override() -> AsyncGenerator[AsyncSession, None]:
        async with functional_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[database_module.get_db] = _get_db_override
    yield
    app.dependency_overrides.pop(database_module.get_db, None)


@pytest.fixture
async def db(
    functional_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Direct session for arranging/asserting DB state inside a test.

    Commits made here are visible to subsequent API requests (separate session).
    """
    async with functional_sessionmaker() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── Authentication helpers ──────────────────────────────────────────────────


def authenticate(client: AsyncClient, user: User) -> None:
    """Attach a valid auth cookie for ``user`` to the client (mints a real JWT)."""
    token = create_access_token(user.id, user.username, user.role.value)
    client.cookies.set(COOKIE_NAME, token)


@pytest.fixture
def login() -> Callable[[AsyncClient, User], None]:
    return authenticate


# ── Model factories ──────────────────────────────────────────────────────────

UserFactory = Callable[..., Awaitable[User]]
StudentFactory = Callable[..., Awaitable[Student]]
GroupFactory = Callable[..., Awaitable[Group]]


@pytest.fixture
def make_group(db: AsyncSession) -> GroupFactory:
    counter = {"n": 0}

    async def _make(name: str | None = None) -> Group:
        counter["n"] += 1
        group = Group(name=name or f"Group-{counter['n']}")
        db.add(group)
        await db.commit()
        await db.refresh(group)
        return group

    return _make


@pytest.fixture
def make_student(db: AsyncSession, make_group: GroupFactory) -> StudentFactory:
    counter = {"n": 0}

    async def _make(
        group: Group | None = None,
        email: str | None = None,
        full_name: str = "Test Student",
    ) -> Student:
        counter["n"] += 1
        if group is None:
            group = await make_group()
        student = Student(
            group_id=group.id,
            email=email or f"student{counter['n']}@example.com",
            full_name=full_name,
        )
        db.add(student)
        await db.commit()
        await db.refresh(student)
        return student

    return _make


@pytest.fixture
def make_user(db: AsyncSession, make_student: StudentFactory) -> UserFactory:
    counter = {"n": 0}

    async def _make(
        role: UserRole = UserRole.TEACHER,
        username: str | None = None,
        password: str = "Sup3rSecret!",
        is_active: bool = True,
        email: str | None = None,
        student: Student | None = None,
    ) -> User:
        counter["n"] += 1
        student_id: int | None = None
        if role == UserRole.STUDENT:
            if student is None:
                student = await make_student()
            student_id = student.id
        user = User(
            username=username or f"user{counter['n']}",
            password_hash=hash_password(password),
            role=role,
            is_active=is_active,
            email=email,
            student_id=student_id,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    return _make


# ── Convenience role + authed-client fixtures ────────────────────────────────


@pytest.fixture
async def teacher(make_user: UserFactory) -> User:
    return await make_user(role=UserRole.TEACHER, username="teacher")


@pytest.fixture
async def admin(make_user: UserFactory) -> User:
    return await make_user(role=UserRole.ADMIN, username="admin")


@pytest.fixture
async def student_user(make_user: UserFactory) -> User:
    return await make_user(role=UserRole.STUDENT, username="student")


@pytest.fixture
async def teacher_client(client: AsyncClient, teacher: User) -> AsyncClient:
    authenticate(client, teacher)
    return client


@pytest.fixture
async def admin_client(client: AsyncClient, admin: User) -> AsyncClient:
    authenticate(client, admin)
    return client


@pytest.fixture
async def student_client(client: AsyncClient, student_user: User) -> AsyncClient:
    authenticate(client, student_user)
    return client
