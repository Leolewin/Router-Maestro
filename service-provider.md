# Router-Maestro Central Service Provider Design

**Status:** implementation specification
**Target host:** Ubuntu Server 24.04 LTS
**Target capacity:** 2 vCPU, 2 GB RAM, 15 GB storage
**Primary use case:** one GitHub account owner accessing the same GitHub Copilot
entitlement from that owner's trusted local Codex, Claude Code, Gemini CLI, or
Router-Maestro installations

## 1. Purpose

This document defines the recommended way to evolve Router-Maestro into a
centralized service provider running on a cloud VM.

The central service owns the GitHub OAuth and short-lived GitHub Copilot
credentials. Remote clients receive separate Router-Maestro inference
credentials and never receive GitHub credentials.

This is an implementation guide for coding agents and maintainers. It describes
the target architecture, security boundaries, state model, code changes,
deployment model, and acceptance criteria. It does not claim that the required
features already exist.

## 2. Non-Negotiable Requirements

1. Exactly one GitHub account may claim the service at a time.
2. A pending or active claim blocks every new GitHub login attempt.
3. The claim remains locked across process and VM restarts.
4. A normal short-lived Copilot bearer-token expiry does not release the claim.
5. The claim is released only after:
   - the pending GitHub device flow reaches its provider-supplied expiry;
   - the active GitHub credential is verified to be irrecoverably expired or
     revoked and has been securely removed; or
   - an authorized administrator explicitly logs out.
6. GitHub OAuth credentials and Copilot bearer tokens remain on the cloud VM.
7. Every local client receives its own revocable, expiring, inference-only
   service token.
8. Inference credentials cannot invoke administration, OAuth, credential,
   configuration, trace, or token-management APIs.
9. Administration is private. It must not be exposed on the public inference
   hostname.
10. The default deployment is private through Tailscale Serve. Public HTTPS is
    an explicitly weaker contingency, not the default.
11. The service runs as one Router-Maestro application and one Uvicorn worker.
    Do not add a second LLM gateway, Redis, Valkey, or PostgreSQL for the initial
    single-VM design.
12. Full request and response body tracing is disabled in centralized
    production.

## 3. Important Usage Boundary

This design assumes all connected client devices are operated by the same
authorized GitHub account holder.

Distinct Router-Maestro service tokens do not turn one GitHub login into
multiple independently licensed GitHub users. Before deployment, the operator
must review the current GitHub and GitHub Copilot terms, subscription rules,
organization policy, and applicable law. Do not resell or share the service
with other people without explicit authorization.

Relevant GitHub terms:

- <https://docs.github.com/en/site-policy/github-terms/github-terms-of-service>
- <https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features>

## 4. Current Authentication Behavior

Router-Maestro currently performs GitHub Copilot authentication as follows:

1. The server starts a GitHub OAuth device flow.
2. The account owner approves the device request on GitHub.
3. The server receives a GitHub OAuth access credential.
4. The server sends that credential to:

   ```text
   https://api.github.com/copilot_internal/v2/token
   ```

5. GitHub returns a short-lived Copilot token and optionally a Copilot API
   endpoint.
6. Router-Maestro sends the short-lived token as a bearer token to Copilot
   endpoints such as `/chat/completions` and `/responses`.
7. Router-Maestro refreshes the short-lived token when necessary.

Therefore:

- the GitHub OAuth credential is the durable upstream credential;
- the Copilot bearer token is a derived, short-lived credential;
- expiration of the derived Copilot token is normal and must not unlock the
  singleton account claim.

Current code references:

- `src/router_maestro/auth/github_oauth.py`
- `src/router_maestro/providers/copilot_support/auth_session.py`
- `src/router_maestro/providers/copilot_support/transport.py`
- `src/router_maestro/server/routes/admin.py`
- `src/router_maestro/server/oauth_sessions.py`

## 5. Architecture Decision

Keep Router-Maestro as one application. Add the service-provider security,
identity, quota, and durable state capabilities inside this repository.

Do not place LiteLLM, One API, New API, oauth2-proxy, Authelia, or another LLM
gateway in front of Router-Maestro for the baseline deployment. Those projects
solve broader multi-tenant or browser-authentication problems and would
duplicate Router-Maestro's protocol translation, streaming, and routing logic.

Use SQLite because this deployment has:

- one VM;
- one Router-Maestro process;
- one Uvicorn worker;
- a small number of owner-controlled clients;
- modest state-write volume;
- no high-availability or multi-region requirement.

Revisit PostgreSQL and a distributed rate-limit store only if multiple active
Router-Maestro replicas become a real requirement.

## 6. Deployment Tiers

### 6.1 Tier 1: Recommended Private Deployment

```text
Trusted local Codex / Claude Code / Gemini / Router-Maestro
  |  unique inference token
  |  Tailscale encrypted Tailnet connection
  v
Tailscale Serve HTTPS on Ubuntu 24.04
  |
  v
127.0.0.1:8080
  |
  v
Router-Maestro container
  |-- one Uvicorn worker
  |-- SQLite/WAL service database
  |-- encrypted GitHub credential
  |-- per-client authorization and quotas
  `-- outbound GitHub/Copilot access
```

Required properties:

- Router-Maestro is published only as `127.0.0.1:8080:8080`.
- No public TCP 80, 443, 8080, metrics, database, Docker API, or admin port.
- Tailscale Serve proxies Tailnet HTTPS to `http://127.0.0.1:8080`.
- Tailscale Funnel is prohibited.
- Tailnet access policy is explicit and tested. Do not rely on the permissive
  behavior of a Tailnet with no ACL policy.
- Tailscale network access does not replace application service tokens.
- Admin access uses a separate admin device over Tailscale SSH, an SSH local
  tunnel, or a distinct private admin listener.

Tailscale ACLs operate on destinations and ports, not HTTP paths. If inference
and admin share one private TCP listener, application authorization remains the
admin boundary. Stronger network isolation requires separate inference and
admin listeners or ports.

References:

- <https://tailscale.com/docs/features/tailscale-serve>
- <https://tailscale.com/docs/features/access-control/acls>
- <https://github.com/tailscale/tailscale>

### 6.2 Tier 2: Hardened Public HTTPS Contingency

Use this tier only when an authorized client cannot install or join Tailscale.

```text
Internet client
  |
  v
Managed CDN/WAF/DDoS edge
  |
  v
Cloud firewall restricted to the managed edge
  |
  v
Host-installed Caddy on Ubuntu 24.04
  |
  v
127.0.0.1:8080 Router-Maestro container

Admin device
  |
  `-- Tailscale or SSH tunnel only; never the public hostname
```

All of the following are mandatory:

1. Managed cloud DDoS protection is enabled.
2. A managed WAF/CDN protects the public hostname where available.
3. The origin firewall prevents arbitrary Internet clients from bypassing the
   managed edge.
4. Edge-to-origin traffic uses authenticated TLS, preferably mTLS or the cloud
   provider's authenticated-origin mechanism.
5. Caddy is installed on the Ubuntu host and managed by systemd.
6. Caddy exposes only model-list and inference routes plus a minimal health
   route.
7. Caddy returns `404` for `/api/admin/*`, `/docs`, `/redoc`,
   `/openapi.json`, `/metrics`, trace, debug, and future management paths.
8. Router-Maestro still binds only to host loopback.
9. Caddy has strict host matching, request-body limits, header/read timeouts,
   SSE-compatible proxy behavior, and bounded logging.
10. The managed edge is tested for long-lived SSE, buffering, HTTP/2, timeout,
    body-size, and disconnect behavior.
11. Per-token and global application limits remain enabled.
12. Forwarded client IP headers are trusted only from documented edge ranges.

Caddy is preferred over the repository's current Traefik deployment for this
single-service contingency because it needs no Docker socket, dynamic Docker
discovery, or public dashboard.

References:

- <https://github.com/caddyserver/caddy>
- <https://caddyserver.com/docs/automatic-https>

## 7. Components Explicitly Rejected for the Baseline

| Component | Decision | Reason |
| --- | --- | --- |
| Traefik | Do not use | Current setup adds public ports, dashboard configuration, dynamic discovery, and a Docker socket mount for one service. |
| nginx | Not needed privately | A strong public-edge alternative, but unnecessary for Tailnet-only access. |
| CrowdSec | Do not use privately | Adds log parsing, scenarios, and remediation components without reducing a private service's primary risk. |
| Fail2ban | Only for unavoidable public SSH | Tailnet-only, key-only SSH should not need it. |
| Redis / Valkey | Do not use | Another network daemon is unnecessary for one process and local state. |
| PostgreSQL | Do not use | Unnecessary memory and operational cost without multiple writers or replicas. |
| LiteLLM | Do not use | Duplicates gateway and routing responsibilities and commonly brings a larger supporting stack. |
| One API / New API | Do not use | Adds a broad multi-user control plane while still requiring custom single-GitHub-account logic. |
| oauth2-proxy / Authelia | Do not use | Browser login/session systems do not solve machine-client tokens or Copilot credential ownership. |

Popular projects evaluated during design research included:

- Tailscale: <https://github.com/tailscale/tailscale>
- Caddy: <https://github.com/caddyserver/caddy>
- nginx: <https://github.com/nginx/nginx>
- Traefik: <https://github.com/traefik/traefik>
- CrowdSec: <https://github.com/crowdsecurity/crowdsec>
- Fail2ban: <https://github.com/fail2ban/fail2ban>
- LiteLLM: <https://github.com/BerriAI/litellm>
- One API: <https://github.com/songquanpeng/one-api>
- New API: <https://github.com/QuantumNous/new-api>

Popularity is an adoption signal, not a security guarantee.

## 8. Security Principals and Credentials

Define three application principals:

| Principal | Allowed capabilities |
| --- | --- |
| `inference` | Model listing, token counting, and inference protocol routes only |
| `admin` | GitHub claim/login/logout, service-token management, runtime configuration, provider management, and controlled diagnostics |
| `metrics` | Metrics endpoint only |

The GitHub/Copilot credential is not a caller principal. It is an encrypted
upstream credential used only by the provider process.

### 8.1 Inference Token Format

Generate at least 256 bits of random secret material:

```text
rm_live_<key-id>_<base64url-random-secret>
```

The plaintext token is displayed exactly once.

Store:

- public random key ID;
- `HMAC-SHA-256(service-token-pepper, complete-presented-token)`;
- scope;
- label;
- enabled/revoked state;
- creation, expiration, and revocation timestamps;
- rate and concurrency policy;
- optional model allowlist;
- pepper key version.

Do not store the plaintext token.

Use HMAC-SHA-256 rather than Argon2id for generated 256-bit machine tokens:

- brute-forcing a genuine 256-bit token is infeasible;
- HMAC supports fast indexed lookup and constant-time verification;
- the external pepper protects a stolen database;
- Argon2id adds avoidable CPU and memory denial-of-service amplification on
  this VM.

Use Argon2id only if a future feature accepts low-entropy human passwords or
recovery passphrases.

Keep the token-HMAC pepper outside SQLite and its backups. It must be distinct
from the GitHub credential encryption key.

### 8.2 GitHub Credential Encryption

Current `auth.json` persistence uses owner-only filesystem permissions but
stores reversible OAuth values as plaintext JSON. Centralized production must
replace that storage for GitHub credentials.

Use an audited AEAD construction such as AES-256-GCM or ChaCha20-Poly1305:

- unique random nonce per encryption;
- credential encryption key version;
- associated data binding provider, record ID, and environment;
- master key stored outside SQLite and backups;
- encrypted VM disk as an additional layer;
- encrypted off-host backups with a separate backup key.

Preferred key source:

1. cloud KMS or managed secret service;
2. systemd encrypted credentials or a root-owned mounted secret file if KMS is
   unavailable.

Do not place long-lived credential keys in the image, Git repository,
`docker-compose.yml`, ordinary `.env`, logs, or `docker inspect` output.

## 9. Durable Single-Account Claim

The current process-local OAuth session map is insufficient. Use a durable
SQLite state machine and transactional compare-and-set operations.

### 9.1 State Machine

```text
EMPTY
  |
  v
AUTHORIZING
  | success
  v
ACTIVE
  | refresh needed
  v
REFRESHING
  | success
  `--------> ACTIVE

AUTHORIZING -- device-flow expiry --> EMPTY
AUTHORIZING -- authorization denied --> AUTHORIZING until device-flow expiry
ACTIVE/REFRESHING -- verified permanent credential failure --> EXPIRED
EXPIRED -- encrypted credential deletion complete --> EMPTY
ACTIVE -- authorized explicit logout --> LOGGING_OUT --> EMPTY
```

Lock rules:

- `AUTHORIZING`, `ACTIVE`, `REFRESHING`, and `LOGGING_OUT` reject a new login
  before requesting a GitHub device code.
- An operator may stop polling or dismiss a local claim UI, but this must not
  release the durable `AUTHORIZING` reservation before its persisted
  device-flow expiry.
- Return `423 Locked` with a generic message.
- Do not reveal GitHub login, email, token lifetime, or pending device details
  to an unauthorized caller.
- `REFRESHING` never releases the lock.
- Network errors, GitHub 5xx responses, rate limits, KMS failures, SQLite
  errors, process restart, and unknown failures never release the lock.
- A permanent credential failure must be specifically classified, such as
  confirmed revocation or repeated unauthorized response after the supported
  renewal path.

### 9.2 Suggested Schema

```sql
CREATE TABLE copilot_claim (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    state TEXT NOT NULL,
    generation INTEGER NOT NULL,
    pending_session_hash BLOB,
    pending_flow_ciphertext BLOB,
    pending_flow_nonce BLOB,
    pending_flow_key_version INTEGER,
    credential_id TEXT,
    github_user_id INTEGER,
    github_login_ciphertext BLOB,
    started_at INTEGER,
    expires_at INTEGER,
    updated_at INTEGER NOT NULL,
    failure_code TEXT
);

CREATE TABLE encrypted_credentials (
    credential_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    ciphertext BLOB NOT NULL,
    nonce BLOB NOT NULL,
    key_version INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE service_tokens (
    key_id TEXT PRIMARY KEY,
    key_hmac BLOB NOT NULL UNIQUE,
    pepper_version INTEGER NOT NULL,
    scope TEXT NOT NULL,
    label_ciphertext BLOB,
    enabled INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at INTEGER,
    rate_profile TEXT NOT NULL
);
```

Additional quota and audit tables can be added through versioned migrations.

Every inference token must have a finite expiration. Enforce a configurable
maximum lifetime at issuance and require rotation before expiry. Centralized
mode must not support non-expiring inference tokens. Emergency break-glass
administration uses a separate short-lived credential procedure and is never
an inference token.

Configure SQLite with:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
```

Use the local VM disk only. Do not place the database on NFS or another network
filesystem.

### 9.3 Claim Flow

1. Require a private admin principal.
2. Start `BEGIN IMMEDIATE`.
3. Verify the singleton row is `EMPTY`, or that an `AUTHORIZING` attempt is
   durably expired.
4. Reserve `AUTHORIZING` and commit before requesting a device code.
5. Request the GitHub device code.
6. Persist only the minimum encrypted pending-flow material.
7. Return the verification URI, user code, opaque session ID, and expiry to
   the admin.
8. Poll GitHub on the server.
9. After OAuth success, call GitHub's authenticated user endpoint and retrieve
   the stable numeric GitHub user ID. Treat the mutable login name as display
   metadata only.
10. In one final transaction, verify the same pending generation still owns the
    singleton row.
11. Encrypt and store the GitHub credential.
12. Transition to `ACTIVE`.
13. Delete pending device-flow secrets.
14. Rebuild the Router generation and pre-warm the Copilot catalog.

The encrypted pending-flow payload contains only the device code, polling
interval, provider-supplied expiration timestamp, and opaque session
reference. It is decrypted only by the server-side poller, is never returned
by a status endpoint, and is deleted atomically on success, provider denial,
expiry, or terminal cleanup. A denial stops polling but retains the singleton
reservation until the original provider-supplied expiry.

If requesting the GitHub device code fails after reserving the slot, transition
the same generation back to `EMPTY`. A concurrent request must never clear a
newer generation's lock.

### 9.4 Logout and Permanent Expiry

Explicit logout must:

1. require the private admin principal and explicit confirmation;
2. transition to `LOGGING_OUT`;
3. stop new inference admission;
4. close and invalidate Copilot provider sessions;
5. delete encrypted GitHub/Copilot credentials;
6. revoke the GitHub authorization when a supported revocation mechanism is
   available;
7. record a metadata-only security event;
8. transition to `EMPTY`.

If cleanup fails, remain locked and alert the operator. Do not silently admit a
replacement account.

Permanent credential invalidation follows the same fail-closed cleanup posture:
stop new inference admission, invalidate cached Copilot sessions, delete the
encrypted credential, record a metadata-only event, and only then transition
from `EXPIRED` to `EMPTY`.

## 10. Route Authorization

Replace the single global `ROUTER_MAESTRO_API_KEY` dependency with explicit
dependencies:

```text
require_inference_principal
require_admin_principal
require_metrics_principal
```

Inference routes:

- `/api/openai/v1/*`
- `/api/openai/beta/v1/responses`
- `/v1/messages`
- `/api/anthropic/v1/*`
- `/api/anthropic/beta/v1/*`
- `/api/gemini/v1beta/*`

Admin routes:

- all `/api/admin/*`;
- GitHub claim, status, and logout;
- service-token create/list/revoke;
- provider and priority mutation;
- trace and diagnostic controls.

There is no claim-cancel endpoint. An operator may stop local polling or
dismiss a claim UI, but the durable `AUTHORIZING` reservation remains locked
until the persisted provider-supplied device-flow expiry.

Preserve compatibility with:

- `Authorization: Bearer <token>`;
- `Authorization: <token>`;
- `x-api-key: <token>`;
- `x-goog-api-key: <token>`.

Normalize all supported headers into one credential validator. Do not log which
raw header contained the successful token.

Return:

- `401` for missing, malformed, unknown, expired, disabled, or revoked
  credentials;
- `403` for an authenticated principal lacking the required scope;
- `423` for a valid admin attempting a GitHub login while the singleton claim
  is locked;
- `429` for quota or concurrency rejection.

## 11. Rate, Concurrency, and Resource Limits

The VM cannot absorb unbounded streaming or authentication traffic.

### 11.1 Host Memory Budget

The fixed 2 GB VM is suitable only for low-concurrency owner-controlled use.
The target is to keep normal steady-state use below approximately 1.3 GiB and
retain at least 300-400 MiB of reclaimable or unused host memory.

| Component | Operating target / cap |
| --- | ---: |
| Ubuntu kernel, systemd, journald, and cloud agent | 300-450 MiB |
| Docker Engine and containerd | 130-220 MiB |
| Tailscale | 30-80 MiB |
| Router-Maestro container | 800 MiB hard cap |
| Tier 2 host Caddy, if enabled | 128 MiB target |
| Reserved headroom | at least 300-400 MiB |

These figures are planning targets rather than additive guaranteed peaks.
Measure the real provider image and request mix before raising any limit.

Do not run local Prometheus, Grafana, Loki, Redis, PostgreSQL, a browser-based
admin UI, or another LLM gateway on this VM. Alert before host memory pressure
causes swapping or OOM events. Prefer no swap on unencrypted storage; if swap
is mandated by the provider, use only encrypted swap with low swappiness and
do not treat it as application capacity.

Initial limits should be conservative and configurable:

| Limit | Initial value |
| --- | ---: |
| Uvicorn workers | 1 |
| Global concurrent streams | 2 initially; raise only after load testing |
| Per-inference-token streams | 1 |
| Global active inference requests | 12 |
| Global inference request-start rate | Configurable bounded token bucket |
| Global admin request-start rate | Configurable bounded token bucket |
| Copilot HTTP max connections | 16 |
| Copilot HTTP keepalive connections | 8 |
| Router-Maestro memory limit | 800 MiB |
| Router-Maestro CPU limit | 1.5 CPU |
| Container PID limit | 128 |
| Maximum request body | 8-16 MiB, configurable |

Implement:

- per-token request-per-minute buckets;
- global inference and admin request-start token buckets, applied as early as
  the ASGI/middleware boundary permits and before upstream work;
- per-token concurrent request and stream leases;
- global request and stream semaphores;
- optional daily/token-usage budgets;
- cancellation-safe lease release;
- finite queue length or immediate `429`, never unbounded waiting;
- strict upstream connect, request, stream-idle, and total deadlines;
- aggressive limits on claim, login-status, logout, and token-management APIs.

Quota accounting that protects cost or account abuse, including per-token
limits and global request-start limits, must fail closed when its durable state
cannot be updated. Do not rely only on in-memory counters that reset after
restart.

Use provider-reported token usage when available. Where exact usage is not
available before execution, reserve a conservative amount and reconcile after
completion.

## 12. SSRF and Outbound Network Controls

Inference callers must never control an upstream URL.

In centralized mode:

- disable custom-provider mutation for inference principals;
- validate provider endpoints on admin write and again before use;
- require HTTPS except for explicitly approved loopback development mode;
- reject URL userinfo and redirects;
- reject localhost, private, link-local, multicast, reserved, and cloud
  metadata addresses;
- resolve DNS and validate every resolved address;
- pin an audited allowlist for GitHub and Copilot hosts;
- block container access to VM metadata endpoints as defense in depth;
- never expose Docker's TCP API.

Reference:

- <https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html>

## 13. Ubuntu 24.04 Host Baseline

### 13.1 Required Services

- Ubuntu Server 24.04 LTS
- Docker Engine and Docker Compose plugin from Docker's official Ubuntu
  repository
- Tailscale from its official repository
- UFW
- AppArmor
- `unattended-upgrades`
- `needrestart`
- `ca-certificates`
- `sqlite3` for controlled backup and integrity operations
- systemd

Do not install a local Prometheus, Grafana, Loki, Redis, PostgreSQL, Kubernetes,
or browser-based admin UI on this VM.

### 13.2 Firewall

Use both the cloud provider firewall and UFW:

```text
default incoming: deny
default routed: deny
default outgoing: allow initially
```

For Tier 1, allow no public application ports. Prefer Tailscale SSH and no
public OpenSSH.

With UFW default-deny ingress, permit Tailnet HTTPS explicitly while leaving
the public interface closed:

```text
ufw allow in on tailscale0 to any port 443 proto tcp
```

If OpenSSH is retained for Tailnet-only break-glass administration, permit it
only on the Tailnet interface:

```text
ufw allow in on tailscale0 to any port 22 proto tcp
```

Do not add equivalent public-interface rules. Tailnet ACLs remain mandatory;
the UFW allowance is not authorization.

If break-glass OpenSSH is required:

- key authentication only;
- root login disabled;
- password authentication disabled;
- restricted source CIDRs or Tailnet interface only;
- explicit allowed users/groups.

Docker-published ports can bypass expected UFW behavior. Therefore:

- publish Router-Maestro only on `127.0.0.1`;
- do not publish database, metrics, admin, debug, or Docker API ports;
- do not disable Docker's firewall management;
- place required Docker forwarding restrictions in the supported
  `DOCKER-USER` path;
- do not install a competing raw nftables ruleset without a separate tested
  Docker firewall design.

References:

- <https://docs.docker.com/engine/install/ubuntu/>
- <https://docs.docker.com/engine/network/packet-filtering-firewalls/>
- <https://ubuntu.com/server/docs/how-to/security/firewalls/>

### 13.3 Patch Policy

- Enable unattended Ubuntu security updates.
- Do not automatically reboot outside a defined maintenance window.
- Alert when `/var/run/reboot-required` exists.
- Upgrade Docker Engine and application images through tested maintenance
  windows.
- Pin production images by immutable digest, not `latest`.
- Do not use automatic unreviewed application image updaters.
- Review Router-Maestro, Python, Docker base image, Tailscale, and optional
  Caddy security updates at least monthly, and expedite critical fixes.

### 13.4 Container Hardening

The production Compose service must include:

```yaml
services:
  router-maestro:
    image: <immutable-image-digest>
    restart: unless-stopped
    ports:
      - "127.0.0.1:8080:8080"
    environment:
      ROUTER_MAESTRO_DEPLOYMENT_MODE: centralized
      ROUTER_MAESTRO_SERVICE_DB: /var/lib/router-maestro/service.db
      ROUTER_MAESTRO_CREDENTIAL_KEY_FILE: /run/router-maestro-secrets/credential-master-key
      ROUTER_MAESTRO_TOKEN_PEPPER_FILE: /run/router-maestro-secrets/service-token-pepper
    user: "1000:1000"
    read_only: true
    tmpfs:
      - /tmp:rw,noexec,nosuid,size=64m
    volumes:
      - type: bind
        source: /var/lib/router-maestro
        target: /var/lib/router-maestro
      - type: bind
        source: /etc/router-maestro/credentials/credential-master-key
        target: /run/router-maestro-secrets/credential-master-key
        read_only: true
      - type: bind
        source: /etc/router-maestro/credentials/service-token-pepper
        target: /run/router-maestro-secrets/service-token-pepper
        read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
      - apparmor:docker-default
    pids_limit: 128
    mem_limit: 800m
    cpus: 1.5
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

`/var/lib/router-maestro` must be the only writable persistent application
mount and must be owned by the container service UID. Secret mounts must be
read-only and readable by the non-root process only. A root-owned `0400` file
is not readable by a UID 1000 container unless the delivery mechanism maps
ownership or group access appropriately. If cloud KMS is used, document the
exact workload-identity and key-delivery mechanism instead of adding key
files.

Required prohibitions:

- no `privileged: true`;
- no host PID or network namespace;
- no Docker socket;
- no public `0.0.0.0:8080`;
- no writable application root filesystem;
- no secrets in image layers or ordinary Compose environment variables;
- no unbounded container logs.

Because Router-Maestro currently requires Python 3.14 while Ubuntu 24.04 does
not provide that as its normal system Python, keep Router-Maestro containerized.

### 13.5 Host Hardening

Keep AppArmor enabled and verify it with `aa-status`.

Use narrowly scoped, tested sysctl settings:

```text
# Docker bridge networking requires IPv4 forwarding for container egress.
# Do not force net.ipv4.ip_forward to 0. Let Docker manage the required
# forwarding and retain its default forward-drop behavior. Apply additional
# Docker traffic restrictions through the documented DOCKER-USER chain.
#
# Do not set net.ipv6.conf.all.forwarding here unless Docker IPv6 and the
# actual Tailnet/cloud topology have been explicitly designed and tested.
net.ipv4.tcp_syncookies = 1
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
net.ipv6.conf.all.accept_source_route = 0
net.ipv6.conf.default.accept_source_route = 0
net.ipv4.conf.all.rp_filter = 2
net.ipv4.conf.default.rp_filter = 2
kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
kernel.yama.ptrace_scope = 1
fs.suid_dumpable = 0
fs.protected_hardlinks = 1
fs.protected_symlinks = 1
```

Do not force Docker-required IPv4 forwarding off. Validate Docker bridge
egress, Tailscale connectivity, and the cloud firewall after every firewall or
sysctl change. Do not enable Docker IPv6 forwarding or disable host IPv6
without a separately tested cloud, Docker, and Tailnet network design. Avoid
unreviewed "hardening" lists that can break Docker, DNS, overlays, or
long-lived streams.

## 14. Logging, Metrics, and Privacy

Centralized production logs may contain:

- timestamp;
- request ID;
- service-token key ID;
- route class;
- selected provider and model;
- status or safe error category;
- latency;
- token usage;
- quota decision;
- claim-state transition.

They must not contain:

- prompts or model output;
- tool arguments or results;
- request or response bodies;
- raw authorization headers;
- service-token plaintext or token prefix sufficient for impersonation;
- GitHub OAuth credentials;
- Copilot bearer tokens;
- GitHub device codes;
- decrypted account metadata;
- unbounded upstream error bodies.

Disable current full-payload audit tracing in centralized production. A future
break-glass diagnostic mode must require a private admin action, have an
automatic short expiry, generate an alert, enforce a byte cap, and retain
redaction.

Keep `/metrics` private and independently authorized. Do not label metrics with
unbounded request paths, raw user IDs, model input, or token secrets.

## 15. Storage, Backup, and Recovery

### 15.1 Disk Budget

Target:

| Category | Budget |
| --- | ---: |
| Ubuntu and packages | 3-4 GiB |
| Docker and two application image versions | 2-3 GiB |
| SQLite and encrypted service state | less than 250 MiB normally |
| Docker logs | 30 MiB |
| journald | 100 MiB |
| temporary local encrypted backups | at most 500 MiB |
| reserved free space | at least 5 GiB |

Alert above 75% disk use. Do not retain full request traces.

### 15.2 Backup

- Use SQLite's online backup API or controlled `.backup`.
- Checkpoint WAL before offline maintenance copies.
- Encrypt backups before they leave the VM.
- Store the backup encryption key separately.
- Never back up plaintext GitHub credentials or service tokens.
- Retain a small policy such as seven daily and four weekly backups.
- Use restricted, versioned, preferably immutable object storage.
- Test restore quarterly on an isolated VM.
- Run `PRAGMA integrity_check` after restore.
- Do not resume a stale `AUTHORIZING` flow from backup.

For Tier 2 only:

- Back up the root-owned Caddy configuration and systemd unit as versioned
  deployment configuration.
- Either back up Caddy's ACME/data directory as encrypted sensitive state, or
  document and test a certificate/account reissuance recovery procedure.
- Treat ACME account keys and certificate private keys as sensitive backup
  material.
- Do not restore Tailscale node state onto a replacement VM; enroll the
  replacement node through the Tailnet's normal authorization process.

If the database, master key, backup key, or VM is suspected compromised, revoke
all service tokens and reauthorize GitHub on a rebuilt VM.

## 16. Required Code Changes

The following file map is a starting point, not a requirement to preserve exact
module names.

### 16.1 New Modules

```text
src/router_maestro/service/
|-- database.py          # SQLite connection, pragmas, migrations, transactions
|-- claims.py            # singleton Copilot claim repository and state machine
|-- credentials.py       # AEAD envelope encryption and key rotation
|-- principals.py        # typed admin/inference/metrics principals
|-- service_tokens.py    # token issue, HMAC verify, revoke, expire
|-- quotas.py            # durable buckets and cancellation-safe leases
|-- audit.py             # metadata-only security and usage events
`-- settings.py          # centralized deployment mode validation
```

### 16.2 Existing Modules to Change

| Area | Required change |
| --- | --- |
| `server/middleware/auth.py` | Replace one global key with typed principal resolution and scope dependencies. |
| `server/app.py` | Apply separate dependencies to inference, admin, and metrics routes; disable wildcard CORS and public docs in centralized mode. |
| `server/routes/admin.py` | Implement private claim/logout/token-management routes using durable transactions. |
| `server/oauth_sessions.py` | Remove token fields and replace the in-memory source of truth with durable pending-attempt state. |
| `auth/github_oauth.py` | Fetch stable GitHub numeric user identity after OAuth; expose no token to clients. |
| `auth/storage.py` | Migrate GitHub credentials away from plaintext JSON for centralized mode. |
| `providers/copilot_support/auth_session.py` | Read encrypted credential through the new repository and map permanent versus transient credential failures correctly. |
| `providers/copilot_support/transport.py` | Reduce connection-pool defaults for the VM and enforce audited upstream destinations. |
| `runtime/request_context.py` | Attach authenticated principal and quota lease to request lifecycle and release on cancellation/final body. |
| `utils/audit.py` | Disable full payload trace or replace it with metadata-only records in centralized mode. |
| `cli/context.py` | Store inference token only in normal client contexts; never reuse an admin credential. |
| `cli/client.py` | Add a separately configured private admin client/context. |
| `README.md` | Explain centralized mode, separate credentials, and the private-first deployment. |
| `docs/deployment.md` | Mark public admin, Docker socket proxying, and public port 8080 as unsupported for centralized mode. |

### 16.3 Deployment Mode

Add an explicit mode such as:

```text
ROUTER_MAESTRO_DEPLOYMENT_MODE=centralized
```

Centralized mode must fail closed at startup if:

- more than one Uvicorn worker is configured;
- the service database cannot be opened securely;
- the token-HMAC pepper is missing;
- the credential encryption key is missing;
- permissions on secret or database files are unsafe;
- wildcard CORS is enabled;
- full-content tracing is enabled;
- an admin credential is missing;
- a public bind is configured without an explicit public-tier acknowledgement;
- plaintext GitHub credentials are detected without completing migration.

## 17. Implementation Phases

### Phase 1: Durable State and Cryptography

- Add SQLite migrations and repositories.
- Add AEAD credential encryption.
- Add singleton claim state machine.
- Migrate existing GitHub credential storage.
- Keep existing external inference behavior unchanged.

### Phase 2: Principal Separation

- Add HMAC-peppered service tokens.
- Split inference, admin, and metrics dependencies.
- Add token create/list/revoke/rotate APIs.
- Add a private admin CLI context.
- Remove inference-token access to `/api/admin/*`.

### Phase 3: Admission and Quotas

- Add body-size limits.
- Add per-token RPM and concurrent-stream limits.
- Add global request and stream limits.
- Add durable usage reconciliation.
- Reduce Copilot connection-pool limits.

### Phase 4: Centralized Production Mode

- Disable CORS, docs, and full tracing by default.
- Add strict startup validation.
- Add metadata-only audit events.
- Add hardened production Compose and systemd files.
- Add Tailscale Serve and Ubuntu 24.04 operator guide.

### Phase 5: Optional Public Contingency

- Add a static host Caddy configuration.
- Add WAF/CDN and origin-lockdown requirements.
- Add external IPv4/IPv6 exposure tests.
- Add SSE behavior tests through the chosen managed edge.

Do not implement the public tier before the private tier and application
authorization controls are complete.

## 18. Required Tests

### Claim and Credential Tests

- Two concurrent login attempts result in one `AUTHORIZING` claim and one
  `423 Locked`.
- No second GitHub device code is requested after a claim is pending or active.
- An active claim survives application and VM restart.
- Pending claim expiration releases only the same pending generation.
- Normal Copilot bearer expiry refreshes without releasing the claim.
- Transient GitHub/Copilot failures do not release the claim.
- Verified permanent credential revocation transitions through cleanup before
  `EMPTY`.
- Logout closes provider resources and deletes encrypted credential state.
- Stored ciphertext does not contain known plaintext credential values.
- OAuth status responses and logs never contain access or refresh tokens.

### Authorization Tests

- Inference token can call models and inference routes.
- Inference token receives `403` on every admin route.
- Admin token is not accepted as an inference token unless explicitly granted
  both scopes.
- Revoked, expired, disabled, malformed, and unknown tokens receive generic
  `401` responses.
- Token validation works through the OpenAI, Anthropic, and Gemini header
  conventions.
- A stolen SQLite database cannot validate tokens without the external pepper.

### Quota and Lifecycle Tests

- Per-token and global concurrency limits are atomic.
- Streaming disconnect releases leases.
- Exceptions and unexpected EOF release leases.
- Restart cannot reset durable daily quota.
- SQLite contention fails closed and never bypasses authorization or quota.
- Oversized requests are rejected before provider work begins.

### Deployment and Security Tests

- Router-Maestro is unreachable on public port 8080 over IPv4 and IPv6.
- Public tier, when enabled, cannot reach admin, docs, metrics, or debug paths.
- Tier 1 has no public application ports.
- Direct public origin access is denied when a managed edge is configured.
- Docker socket is not mounted.
- Container runs without added Linux capabilities and with a read-only root.
- No secret appears in `docker inspect`, logs, audit files, metrics, or backup.
- Backup restore preserves an active lock but does not resume an expired
  pending flow.

## 19. Operational Runbook

### First Claim

1. Connect from the private admin device.
2. Confirm claim state is `EMPTY`.
3. Start the GitHub claim.
4. Confirm state changes to `AUTHORIZING`.
5. Complete GitHub authorization directly in the account owner's browser.
6. Confirm state changes to `ACTIVE`.
7. Confirm a second login returns `423 Locked`.
8. Confirm GitHub credentials are absent from client files and responses.

### Client Provisioning

1. Create one labeled inference token for one local installation.
2. Assign expiration, RPM, stream concurrency, and optional model policy.
3. Deliver the plaintext token once over an approved secret channel.
4. Configure the local Router-Maestro context or Codex provider.
5. Verify model listing and one streaming request.
6. Verify the same token cannot call an admin endpoint.

### Monitoring

Monitor and alert on:

- claim-state transitions;
- failed admin authorization;
- repeated `401`, `403`, and `429` by key ID;
- Copilot `401`, `403`, `429`, and 5xx rates;
- credential decrypt errors;
- rejected outbound destinations;
- active stream count;
- memory, CPU, disk, and file-descriptor use;
- backup failure;
- firewall or Tailnet policy drift;
- pending Ubuntu reboot;
- disk use above 75%.

### Suspected Compromise

1. Remove the VM from Tailnet access or block it at the cloud firewall.
2. Stop Router-Maestro.
3. Preserve only encrypted state and metadata logs required for investigation.
4. Revoke the GitHub authorization from a known-clean device.
5. Revoke all service tokens.
6. Rotate token pepper, credential key, backup key, and Tailnet keys.
7. Rebuild a fresh VM from trusted images.
8. Restore only verified encrypted data that is still required.
9. Complete a new GitHub authorization after the new host passes security
   checks.
10. Do not treat a container restart as sufficient remediation for a suspected
    host compromise.

## 20. Definition of Done

The centralized service-provider feature is complete only when:

- one GitHub account claim is durable and transactionally exclusive;
- all GitHub/Copilot credentials are server-only and encrypted at rest;
- inference and administration have separate credentials and route policies;
- every client token is individually revocable and quota-bound;
- the default deployment has no public HTTP service;
- the Ubuntu 24.04 host and container configuration fail closed;
- full-content tracing is disabled;
- streaming cancellation and failures release every quota and Router lease;
- backup and restore are tested;
- the security and concurrency tests above pass;
- documentation states the GitHub account-sharing and subscription boundary;
- the public Caddy tier, if implemented, remains optional and cannot expose the
  admin plane.

## 21. Final Recommendation

Implement this as an internal evolution of Router-Maestro, not as a second large
application:

```text
Ubuntu 24.04
  + Tailscale Serve and explicit ACLs
  + localhost-only hardened Router-Maestro container
  + one Uvicorn worker
  + SQLite/WAL
  + encrypted singleton GitHub credential
  + separate HMAC-peppered inference/admin/metrics credentials
  + per-client and global admission controls
```

Only add host-installed Caddy and a managed WAF/CDN when a specific trusted
client cannot use Tailscale. Do not expose the administration plane publicly in
either tier.
