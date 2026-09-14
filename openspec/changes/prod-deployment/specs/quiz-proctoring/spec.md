## ADDED Requirements

### Requirement: Proctoring snapshots are served only to authorized teachers

Webcam evidence frames SHALL NOT be publicly readable. Object storage MUST NOT be reachable from the internet, and stored objects MUST NOT be granted a public-read ACL. Snapshots SHALL be retrievable only through an authenticated application endpoint that verifies the requester is permitted to see that subject's evidence before streaming the object.

#### Scenario: Authorized teacher views a snapshot

- **WHEN** a teacher who owns the subject requests a snapshot belonging to one of that subject's attempts
- **THEN** the image SHALL be returned with its stored content type

#### Scenario: A teacher from another subject is refused

- **WHEN** a teacher who does not own the subject requests one of its snapshots
- **THEN** the response SHALL be 403 and no image data SHALL be returned

#### Scenario: A student cannot read evidence

- **WHEN** a student requests a snapshot endpoint
- **THEN** the request SHALL be refused

#### Scenario: An anonymous request is refused

- **WHEN** a request arrives with no authenticated session
- **THEN** the request SHALL be refused and no image data SHALL be returned

#### Scenario: Stored objects carry no public ACL

- **WHEN** a snapshot is uploaded to object storage
- **THEN** it SHALL be written without a public-read ACL, so possession of the object key alone does not grant access

#### Scenario: Object storage is not published

- **WHEN** the production stack is running
- **THEN** the object storage service SHALL NOT be exposed through the reverse proxy or bound to a host port

### Requirement: Teacher-facing evidence links address the application, not object storage

The teacher review view SHALL link to snapshots by their application endpoint rather than by an object-storage URL, so that evidence links cannot outlive a viewer's authorization.

#### Scenario: Review page renders application-addressed links

- **WHEN** the teacher assignment view lists proctoring evidence
- **THEN** each link SHALL address the application's authenticated snapshot endpoint and SHALL NOT contain an object-storage host

#### Scenario: Upload response does not leak a durable public URL

- **WHEN** a snapshot upload succeeds
- **THEN** the response SHALL confirm storage without returning a publicly fetchable object-storage URL
