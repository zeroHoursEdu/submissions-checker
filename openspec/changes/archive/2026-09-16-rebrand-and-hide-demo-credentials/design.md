# Design

## Context

The demo hint is a single i18n string rendered unconditionally by `templates/login.html`.
The seeded accounts behind it are already gated: `0002`, `0003` and `0007` each check
`os.environ.get("ENVIRONMENT", "production") == "development"` before inserting anything,
and `docker-compose.prod.yml` sets `ENVIRONMENT: production` for both `app` and `migrate`.
So production has no such accounts. Only the advertisement leaked.

That gating is worth preserving rather than replacing. It defaults to production, which is
the safe direction: a misconfigured environment seeds nothing instead of seeding everything.

## Goals / Non-Goals

**Goals.** One product name on every user-visible surface. No credentials on a production
sign-in page. A documented way into a system with no accounts.

**Non-Goals.** Moving seeding out of migrations. It is unusual to seed from a migration, but
these are gated, defaulted safely, and already applied everywhere that matters; rewriting them
into a seed command would churn the migration history for no change in behaviour. Renaming the
repository, image or Python package — those are already `submissions-checker`.

## Decisions

### Gate the hint on the application's environment, not on the template

`render()` injects `vocab` and `available_languages` into every template. Adding
`is_development` there makes it available to every page, and keeps the login route from
having to know why it is passing a flag.

The alternative — deleting the string outright — was rejected because the hint is genuinely
useful locally, and a developer who cannot remember the seeded password will otherwise go
read a migration to find it.

The condition is `settings.is_development`, the same predicate the migrations use, so the
hint appears exactly where the accounts it describes exist. Tying it to anything else would
let the two drift apart, which is how the current state came about.

### Rename in place, including the i18n key

`sign_in_to_edutrack` becomes `sign_in_to_app`. Leaving the key while changing its value
would mean the next person grepping for `edutrack` finds a key that no longer describes
anything. The Ukrainian vocabulary file is the only locale.

### Document the manual account, do not build a command for it

An operator needs this once per deployment. A CLI subcommand would be more code to test and
maintain than the three SQL statements it would wrap, and the operator already has a database
client in hand. The documentation has to cover two things that are easy to get wrong: bcrypt
is the hash the application verifies, and `ck_users_student_role_has_student_id` requires a
`STUDENT` row to carry a `student_id`, so a teacher account is the simpler first account.

## Risks / Trade-offs

**A template flag can be forgotten on a new page.** Any future page that wants to show
development-only content must remember to check it. Accepted: the alternative is per-route
plumbing, and the flag is injected centrally so it is always available.

**`ENVIRONMENT` remains load-bearing for more than logging.** It already decides whether
accounts are seeded; it now also decides whether the sign-in page discloses them. That is a
concentration of meaning in one variable, but it is the same meaning in both places, and
splitting it would create a second variable that can disagree with the first.

## Migration Plan

No schema change. The rename is text; the hint is a template condition. Deployment is the
ordinary rolling release — both versions serve the same database, and neither reads anything
the other writes.

## Open Questions

None.
