## ADDED Requirements

### Requirement: Production stack is defined separately from development

The system SHALL provide a `docker-compose.prod.yml` that composes the production runtime: a Caddy reverse proxy, two or more replicas of the application, PostgreSQL, MinIO, Watchtower, and a backup service. It MUST NOT bind-mount source code, MUST NOT enable application reload, MUST NOT include LocalStack, and MUST NOT carry default credentials. All secrets and host-specific values SHALL be supplied through an environment file that is never committed.

#### Scenario: Production compose refuses placeholder configuration

- **WHEN** the stack is started with `SECRET_KEY` left at its example value
- **THEN** the application SHALL fail to start with an explicit error about a placeholder secret, rather than serving traffic with a weak key

#### Scenario: No source bind mounts in production

- **WHEN** `docker-compose.prod.yml` is inspected
- **THEN** no application service SHALL mount `./src`, `./tests`, `./templates`, `./static`, or `./alembic` from the host, so the running code is exactly the contents of the published image

#### Scenario: Example environment file lists every required variable

- **WHEN** an operator copies `.env.prod.example` and fills in every value it names
- **THEN** the stack SHALL start without any further environment configuration

### Requirement: TLS is terminated by Caddy with automatic certificates

The system SHALL terminate HTTPS at Caddy, which obtains and renews certificates automatically for a configured domain. Caddy SHALL be the only service publishing ports to the host. Application replicas, PostgreSQL and MinIO MUST NOT publish ports to the host and SHALL be reachable only on the internal Docker network.

#### Scenario: Certificate issued on first start

- **WHEN** the stack starts with `DOMAIN` set to a hostname whose DNS A record resolves to the host
- **THEN** Caddy SHALL obtain a certificate and serve the application over HTTPS without operator intervention

#### Scenario: Plain HTTP is redirected

- **WHEN** a client requests the site over HTTP
- **THEN** Caddy SHALL redirect to HTTPS

#### Scenario: Backing services are not exposed

- **WHEN** the host's published ports are enumerated while the stack runs
- **THEN** only Caddy's HTTP and HTTPS ports SHALL be bound, and PostgreSQL's and MinIO's ports SHALL NOT be reachable from outside the host

### Requirement: Traffic is load-balanced across application replicas

Caddy SHALL distribute requests across all healthy application replicas. It SHALL re-resolve the application service name periodically so replicas that are added, removed or replaced are discovered without a Caddy restart or configuration reload.

#### Scenario: New replica receives traffic without a reload

- **WHEN** an application replica is replaced and registers a new container IP under the same service name
- **THEN** Caddy SHALL begin routing to the new address within its configured refresh interval, without being restarted

#### Scenario: Requests are spread across replicas

- **WHEN** multiple requests arrive while two replicas are healthy
- **THEN** the requests SHALL be distributed across both replicas rather than pinned to one

### Requirement: Every service is bounded by an explicit memory limit

Each service in the production stack SHALL declare a hard memory limit sized for a 2GB host, and the sum of those limits SHALL leave headroom for transient check-sandbox containers. The application SHALL run a single worker per replica with a bounded database connection pool.

#### Scenario: A runaway service cannot starve the database

- **WHEN** one application replica's memory use grows without bound
- **THEN** that replica SHALL be terminated at its own limit, and PostgreSQL SHALL continue serving the remaining replica

#### Scenario: Connection pool fits the database's connection ceiling

- **WHEN** every replica saturates its connection pool at the same time as the migration job and the backup job
- **THEN** the total connections opened SHALL remain below PostgreSQL's configured `max_connections`

### Requirement: The application reaches the Docker socket as a non-root user

The production image SHALL run as a non-root user. Because check sandboxes are launched through the host Docker daemon, the application container SHALL be granted socket access by supplementary group membership matching the host's Docker group, supplied as configuration rather than baked into the image.

#### Scenario: Sandbox launches without running the app as root

- **WHEN** a check runs in the production stack with `DOCKER_GID` set to the host's Docker group id
- **THEN** the sandbox container SHALL start successfully and the application process SHALL NOT be uid 0

#### Scenario: Misconfigured group is diagnosable

- **WHEN** `DOCKER_GID` does not match the host's Docker group
- **THEN** the failure SHALL surface as a permission error on the Docker socket in the application logs, not as a silent check failure

### Requirement: Sandbox bind mounts remain host-resolvable in production

Paths bind-mounted into check sandboxes SHALL resolve identically in the application container and on the host daemon, because the daemon interprets those paths on the host. The production stack SHALL mount the sandbox working directory at the same path inside and outside the container, and SHALL supply the plugins directory as a host-absolute path.

#### Scenario: Sandbox reads the submission and plugin trees

- **WHEN** a check runs in the production stack
- **THEN** the sandbox container SHALL see the submission at `/submission` and the plugin at `/plugin`, both populated, because the host daemon resolved the source paths successfully

#### Scenario: Sandbox writes its result

- **WHEN** a subject image running as a non-root user writes `result.json` to `/output`
- **THEN** the application SHALL read that file back after the container exits
