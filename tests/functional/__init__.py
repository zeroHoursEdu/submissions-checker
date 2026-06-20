"""Functional (API-level) tests driving the real FastAPI app over ASGI.

These exercise routes end-to-end against a real PostgreSQL container with the
authentication/authorization stack fully active, but with the ``get_db``
dependency overridden to point at the test database. No HTTP server, browser,
or external services are required.
"""
