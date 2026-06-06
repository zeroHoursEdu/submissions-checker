# Submissions Checker

Automated student code submission checker with GitHub integration, test execution, and AI-powered code review.

## Overview

The Submissions Checker is a Python-based monolithic application that automates the process of checking student code submissions. It integrates with GitHub to receive pull request webhooks, executes automated tests, performs AI-powered code reviews, and provides feedback directly on GitHub PRs.

### Key Features

- **GitHub Integration**: Receive and process pull request webhooks
- **Automated Testing**: Execute CLI-based tests on student submissions
- **AI Code Review**: Leverage AI providers (OpenAI, Azure OpenAI) for intelligent code analysis
- **Reliable Processing**: Transactional outbox pattern ensures no events are lost
- **Async Architecture**: Built with FastAPI and async/await for high performance
- **Background Jobs**: In-process scheduled tasks with APScheduler
- **Type Safety**: Comprehensive type hints and Pydantic validation

## Architecture

### Technology Stack

- **Web Framework**: FastAPI (async, modern, OpenAPI documentation)
- **Database**: PostgreSQL 16+ with asyncpg driver
- **ORM**: SQLAlchemy 2.0+ with async support
- **Migrations**: SQL-based migrations (runs automatically on app startup)
- **Background Jobs**: APScheduler (in-process async scheduler)
- **HTTP Client**: httpx (async)
- **Dependency Management**: uv (fastest Python package manager)
- **Configuration**: Pydantic Settings
- **Testing**: pytest + testcontainers
- **Code Quality**: Ruff (linting + formatting)

### Architecture Patterns

**Transactional Outbox Pattern**
- Events are written to the `outbox_messages` table in the same transaction as business logic
- Scheduled job polls for unprocessed messages every 10 seconds
- Messages are dispatched to appropriate async task handlers in-process
- Ensures reliable event processing without message loss

**Async-First Design**
- FastAPI handles requests asynchronously
- SQLAlchemy async sessions for non-blocking database operations
- httpx for async HTTP requests
- APScheduler with AsyncIOScheduler for background task processing

**Separation of Concerns**
- **API Layer**: Request/response handling, validation (FastAPI routes)
- **Core Layer**: Configuration, database, logging, security
- **Service Layer**: Business logic for GitHub, AI, testing
- **Data Layer**: Database models and queries
- **Worker Layer**: Background task processing

## Project Structure

```
submissions-checker/
├── src/submissions_checker/       # Application source code
│   ├── api/                       # API routes and schemas
│   │   ├── routes/               # FastAPI route handlers
│   │   └── schemas/              # Pydantic request/response schemas
│   ├── core/                     # Core infrastructure
│   │   ├── config.py            # Pydantic Settings configuration
│   │   ├── database.py          # SQLAlchemy async setup
│   │   ├── logging.py           # Structured logging (structlog)
│   │   └── security.py          # Security utilities
│   ├── db/                       # Database layer
│   │   ├── models/              # SQLAlchemy models
│   │   ├── base.py              # Base model class
│   │   └── session.py           # Session management
│   ├── services/                 # Business logic services
│   │   ├── github/              # GitHub API integration
│   │   ├── ai/                  # AI provider integration
│   │   ├── testing/             # Test execution
│   │   └── user_service.py      # User operations
│   ├── workers/                  # Background job processing
│   │   ├── tasks/               # Background task definitions
│   │   └── scheduled/           # Scheduled jobs (outbox processor)
│   ├── utils/                    # Utility functions
│   └── main.py                   # FastAPI application entry point
├── tests/                        # Test suite
│   ├── integration/             # Integration tests
│   └── unit/                    # Unit tests
├── migrations/                   # Database migrations
│   └── sql/                     # SQL migration files
├── docker/                       # Dockerfiles
├── scripts/                      # Utility scripts
├── docker-compose.yml           # Development environment
└── pyproject.toml               # Project dependencies
```

## Getting Started

### Prerequisites

- Python 3.12+
- Docker and Docker Compose
- [uv](https://github.com/astral-sh/uv) package manager
- PostgreSQL 16+ (via Docker)

### Quick Setup

1. **Install uv** (if not already installed):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd submissions-checker
   ```

3. **Run the setup script**:
   ```bash
   ./scripts/dev_setup.sh
   ```

   This will:
   - Install dependencies
   - Create `.env` file from template
   - Start Docker services (PostgreSQL only)
   - Migrations will run automatically on app startup

4. **Configure environment variables**:
   Edit `.env` and update:
   - `SECRET_KEY`: Generate a secure secret key
   - `GITHUB_WEBHOOK_SECRET`: Your GitHub webhook secret
   - `OPENAI_API_KEY`: Your OpenAI API key (if using AI features)

5. **Start the development server** (migrations run automatically on startup):
   ```bash
   make dev
   ```

   Access the application at:
   - API: http://localhost:8000
   - API Documentation: http://localhost:8000/docs
   - Health Check: http://localhost:8000/health

### Manual Setup

If you prefer manual setup:

```bash
# Install dependencies
make install

# Start Docker services (PostgreSQL)
make up

# Start development server (migrations run automatically)
make dev
```

## Development

### Available Commands

```bash
make help              # Show all available commands
make install           # Install dependencies
make dev               # Start development server with hot reload
make up                # Start all Docker services
make down              # Stop all Docker services
make test              # Run tests with coverage
make migrate           # Run database migrations
make migrate-create    # Create new migration
make lint              # Run linting
make format            # Format code
make clean             # Clean up generated files
```

### Running Tests

```bash
# Run all tests with coverage
make test

# Run integration tests only
make test-integration

# Run unit tests only
make test-unit

# Run tests in watch mode
make test-watch
```

### Code Quality

```bash
# Run linting
make lint

# Auto-fix linting issues
make lint-fix

# Format code
make format

# Check formatting (CI)
make format-check

# Type checking
make type-check

# Run all quality checks
make quality
```

## Database Migrations

Simple SQL-based migration system.

### How It Works

- Migrations stored in `migrations/sql/`
- Naming: `{sequence}_{description}.sql` (e.g., `003_create_users.sql`)
- Run automatically on application startup
- Tracked in `schema_migrations` table
- Idempotent: safe to run multiple times
- Checksums prevent modification of executed migrations

### Creating New Migrations

1. Create SQL file with next sequence number:
   ```bash
   touch migrations/sql/003_create_users.sql
   ```

2. Write SQL DDL:
   ```sql
   -- Migration: 003_create_users
   -- Description: Create users table
   -- Date: 2026-02-15

   CREATE TABLE IF NOT EXISTS users (
       id SERIAL PRIMARY KEY,
       email VARCHAR(255) NOT NULL UNIQUE,
       created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
   );

   CREATE INDEX IF NOT EXISTS idx_users_email
       ON users(email);
   ```

3. Restart application - migration runs automatically

### Best Practices

- Use `CREATE TABLE IF NOT EXISTS` for idempotency
- Use `CREATE INDEX IF NOT EXISTS` for indexes
- Never modify executed migrations (checksum validation fails)
- Add comments with migration number, description, and date
- Test in development first
- Keep migrations sequential (001, 002, 003...)

### Database Operations

```bash
# Migrations run automatically on app startup
make dev

# View migration status (query schema_migrations table)
make db-shell
# Then: SELECT * FROM schema_migrations ORDER BY executed_at;

# Open PostgreSQL shell
make db-shell
```

## Configuration

Configuration is managed via environment variables using Pydantic Settings. See `.env.example` for all available options.

### Key Configuration Options

- `ENVIRONMENT`: `development`, `test`, or `production`
- `DATABASE_URL`: PostgreSQL connection string
- `SCHEDULER_ENABLED`: Enable/disable background scheduler (default: `true`)
- `GITHUB_WEBHOOK_SECRET`: Secret for validating GitHub webhooks
- `OPENAI_API_KEY`: OpenAI API key for AI code review
- `LOG_LEVEL`: Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`)

## API Endpoints

### Health Checks

- `GET /health` - Basic health check
- `GET /health/ready` - Readiness check with database connectivity

### Webhooks (Skeleton)

- `POST /webhooks/github` - GitHub webhook handler

### Users (Skeleton)

- `POST /api/v1/users` - Create user
- `GET /api/v1/users/{user_id}` - Get user

Full API documentation is available at `/docs` when the server is running.

## Background Jobs

The application uses APScheduler for in-process background job processing:

- **PR Processing**: Handle GitHub PR webhooks (triggered via outbox)
- **Test Execution**: Run CLI tests on submissions (triggered via outbox)
- **AI Review**: Perform AI-powered code review (triggered via outbox)
- **Outbox Processor**: Process transactional outbox messages (scheduled, runs every 10 seconds)

## Deployment

### Docker

Build and run with Docker Compose:

```bash
docker-compose up -d
```

Services:
- `app`: FastAPI application with in-process scheduler (port 8000)
- `postgres`: PostgreSQL database (port 5432)

Migrations run automatically when the app starts.

### Production Considerations

1. **Environment Variables**: Set production values in `.env` or environment
2. **Database**: Use managed PostgreSQL service or dedicated instance
3. **Secrets**: Store sensitive data in secrets management system
4. **Monitoring**: Add Prometheus metrics and health check monitoring
5. **Logging**: Configure structured logging output to log aggregation service
6. **Scaling**: Run multiple app instances behind load balancer (outbox pattern handles concurrent processing)

## Development Status

**Current Phase**: Backbone Implementation

This is the foundational infrastructure phase. The following are implemented:

✅ **Complete**
- Project structure and configuration
- Monolithic architecture with in-process background jobs
- Docker development environment (2 services: app + postgres)
- Core application modules (config, logging, database, scheduler, migrations)
- Database models (base classes, outbox pattern)
- SQL-based migrations (run automatically on startup)
- FastAPI application with health endpoints
- Service layer skeletons (GitHub, AI, testing)
- Background job infrastructure (APScheduler, task skeletons)
- Testing infrastructure (pytest, testcontainers)
- Development tools (Makefile, scripts)

🚧 **To Be Implemented**
- User and Submission database models
- GitHub integration (PR cloning, webhooks, comments)
- Test execution (CLI runner, result parsing)
- AI integration (code review, test analysis)
- User authentication and authorization
- Complete API endpoints
- Background job implementations
- CI/CD pipelines
- Monitoring and metrics

## Contributing

### Code Style

- Follow PEP 8 conventions
- Use type hints for all functions
- Write docstrings for public APIs
- Keep functions focused and small
- Use Ruff for linting and formatting

### Testing

- Write tests for new features
- Maintain test coverage above 80%
- Use testcontainers for integration tests
- Mock external services in unit tests

### Pull Requests

- Create feature branches from `main`
- Write descriptive commit messages
- Update tests and documentation
- Ensure all quality checks pass

## Running E2E Tests

End-to-end tests use Playwright + pytest-bdd (Gherkin/BDD). They spin up an isolated Docker stack (separate DB, app, S3) so they never touch your dev environment.

### Prerequisites

```bash
# Install e2e dependencies
uv pip install -e ".[e2e]"

# Install Playwright browsers
playwright install chromium
```

### Quick start

```bash
make e2e                   # headless (CI-friendly)
make e2e-headed            # browser visible (debug mode)
```

### Options

| Option | Example | Effect |
|--------|---------|--------|
| `TAGS` | `make e2e TAGS=@smoke` | Run only scenarios with that tag |
| `SCENARIO` | `make e2e SCENARIO="teacher login"` | Run scenarios matching name substring |
| `FILE` | `make e2e FILE=tests/e2e/features/teacher_auth.feature` | Run a single feature file |

**Example: run only the auth feature in headed mode**
```bash
make e2e-headed FILE=tests/e2e/features/teacher_auth.feature
```

### Stack management

```bash
make e2e-up      # start stack without running tests
make e2e-down    # tear down the stack
make e2e-logs    # tail app logs
```

### Architecture

- App runs on `http://localhost:8001`
- Isolated Postgres on port `5435` (DB: `submissions_checker_e2e`)
- LocalStack S3 on port `4567`
- All state is ephemeral — `make e2e-down` destroys it

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `E2E_APP_URL` | `http://localhost:8001` | App base URL |
| `E2E_DB_URL` | `postgresql://postgres:postgres@localhost:5435/submissions_checker_e2e` | Direct DB connection for fixtures |

## License

See [LICENSE](LICENSE) file for details.

## Support

For issues, questions, or contributions, please open an issue on GitHub.

---

Built with ❤️ using FastAPI, SQLAlchemy, and modern Python practices.
