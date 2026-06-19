## REMOVED Requirements

### Requirement: GitHub webhook authentication
**Reason**: The `POST /webhooks/github` endpoint is being removed; submissions are
ingested exclusively via authenticated ZIP upload, so there is no webhook to
authenticate.
**Migration**: None. No webhook endpoint remains. Existing submissions are
unaffected; new submissions must be made through the student portal ZIP upload.

The GitHub webhook endpoint SHALL reject requests whose HMAC-SHA256 signature
does not match before processing the payload.

#### Scenario: Forged webhook rejected
- **WHEN** a webhook arrives with a missing or invalid `X-Hub-Signature-256`
- **THEN** the endpoint returns 401 and enqueues no job
