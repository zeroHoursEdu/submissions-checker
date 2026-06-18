# Spec: Auth Security

## Purpose

Defines startup secret enforcement, live session validation, secure cookie
attributes, and webhook authentication for the application.

## Requirements

### Requirement: Strong signing secret enforced at startup
The application SHALL refuse to start when `SECRET_KEY` is a known placeholder
or, in production, an obviously weak value.

#### Scenario: Placeholder secret rejected
- **WHEN** the app starts with `SECRET_KEY=your-secret-key-here-change-in-production`
- **THEN** startup fails with a configuration error

### Requirement: Live session validation
Every authenticated request SHALL be validated against the current user record.

#### Scenario: Deactivated user is rejected
- **WHEN** a user with a still-unexpired JWT is set `is_active = false`
- **THEN** their next authenticated request returns 401

#### Scenario: Deleted user is rejected
- **WHEN** the user record referenced by a valid JWT no longer exists
- **THEN** the request returns 401

### Requirement: Secure auth cookie
The auth cookie SHALL be marked `Secure` in production and remain `HttpOnly`
and `SameSite=Strict`.

#### Scenario: Production cookie is secure
- **WHEN** the app runs with `ENVIRONMENT=production` and sets the auth cookie
- **THEN** the cookie has the `Secure` attribute

### Requirement: GitHub webhook authentication
The GitHub webhook endpoint SHALL reject requests whose HMAC-SHA256 signature
does not match before processing the payload.

#### Scenario: Forged webhook rejected
- **WHEN** a webhook arrives with a missing or invalid `X-Hub-Signature-256`
- **THEN** the endpoint returns 401 and enqueues no job
