# product-identity Specification

## Purpose
The product presents one name everywhere; demo credentials are never advertised outside development.
## Requirements
### Requirement: The product presents one name

Every user-visible surface SHALL identify the product by a single name. A person who receives an email from the system SHALL be able to connect it to the site they sign in to, without being told they are the same thing.

#### Scenario: Sign-in page names the product

- **WHEN** a user opens the sign-in page
- **THEN** the heading SHALL name the product, and that name SHALL be the same one used everywhere else

#### Scenario: Account emails name the product

- **WHEN** the system sends credentials, a password reset, or a notification
- **THEN** the message SHALL identify the product by the same name the interface uses

### Requirement: Credential hints are confined to development

The sign-in page SHALL NOT display seeded account credentials outside a development environment. The hint exists to spare a developer from looking up passwords; a production sign-in page SHALL disclose no usernames.

#### Scenario: Production sign-in page discloses nothing

- **WHEN** the sign-in page is served by an application configured for production
- **THEN** no account name and no password SHALL appear anywhere in the response

#### Scenario: Development sign-in page keeps the hint

- **WHEN** the sign-in page is served by an application configured for development
- **THEN** the seeded accounts SHALL be shown, so a developer can sign in without consulting the source

