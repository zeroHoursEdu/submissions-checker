# local-development Specification

## Purpose
What a fresh developer checkout must provide: seeded demo accounts, documented in the README and surfaced on the login page in development.
## Requirements
### Requirement: Seeded local accounts are documented

The project's README SHALL state which accounts exist after setting up a local environment, what their credentials are, and that they do not exist in production. A developer SHALL NOT have to read a migration to find out how to sign in.

#### Scenario: A new contributor signs in locally

- **WHEN** a contributor has brought up the local stack for the first time
- **THEN** the README SHALL tell them an account to sign in with

#### Scenario: The reader is told these are local only

- **WHEN** a reader finds the seeded credentials in the README
- **THEN** the documentation SHALL state that they are absent from a production deployment, so nobody assumes production is exposed

### Requirement: An operator can create the first account without the application

The deployment documentation SHALL describe how to insert a working account directly into the database. A freshly deployed production system seeds no accounts, so there SHALL be a documented way in that does not require an account to already exist.

The procedure SHALL produce a password hash in the format the application verifies, and SHALL satisfy the constraints the schema places on an account's role.

#### Scenario: First administrator on a new deployment

- **WHEN** an operator has deployed a system whose `users` table is empty
- **THEN** the documentation SHALL let them create an account and sign in with it

#### Scenario: Student accounts require a student record

- **WHEN** the documented procedure is used to create a student account
- **THEN** it SHALL account for the schema's requirement that a student account reference a student record

