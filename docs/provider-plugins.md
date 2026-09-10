# Provider Plugins

Router-Maestro uses an explicit in-process `ProviderRegistry`. Bundled and
application-supplied providers use the same registration contract. There is no
automatic package discovery, remote installation, or plugin hot loading.

## Registration

A `ProviderPlugin` declares:

- `id`: the provider component of public `provider/model` IDs.
- `factory`: a synchronous constructor returning a fresh `BaseProvider` instance.
- `auth`: non-secret `ProviderAuthDefinition` metadata for authentication discovery.
- `endpoints`: optional `ProviderEndpoint` declarations for provider-owned resources.

Constructors must be resource-lazy: create HTTP clients and other external
resources only when used, and implement `close()` to release them. A factory must
return the declared provider ID and must not reuse an instance from a previous
Router generation. Credentials stay in the provider and its existing credential
repository, never in endpoint declarations.

An application integrates a plugin by supplying a registry at startup:

```python
from router_maestro.providers.registry import default_provider_registry
from router_maestro.server.app import create_app
from my_provider import plugin

registry = default_provider_registry().with_plugins(plugin)
app = create_app(provider_registry=registry)
```

`with_plugins()` returns a new snapshot; it never modifies other applications or
the default registry. Duplicate provider IDs are rejected case-insensitively.
Configured provider instances cannot shadow registered provider IDs.

The same registry is used for HTTP routes, authentication discovery, initial
Router construction, and subsequent Router generations. Configured provider
types can use `registry.with_configured_factory(type_name, factory)`. Such a
factory receives the instance name, `CustomProviderConfig` (including extra
options), and `CredentialRepository`. The bundled `openai-compatible` type uses
this mechanism and retains its existing environment/repository/anonymous
credential policy. The plugin set and endpoint declarations are fixed for the
application lifetime; changing them requires building a new application.

Authentication metadata advertises the existing API-key and Copilot OAuth
flows. It is not a general OAuth-flow plugin API; a new OAuth implementation
requires a separately designed authentication integration.

## Generation bindings

A provider implements `list_models()` and `is_authenticated()` and declares its
transports through `bindings()`. It does not have to implement legacy Chat DTO
methods. By default, its public operation capabilities derive from its bindings.
Legacy Chat/Responses method implementations remain supported by the legacy
execution adapter.

Each `EndpointBinding` contains a stable ID, `WireProtocol`, operation
capabilities, `ProviderDialect`, and HTTP executor. Shared protocol codecs own
all standard request/response/event translation:

```text
HTTP ingress → model selection → shared transport policy
             → identity payload or source codec → semantic IR → target codec
             → dialect.prepare_attempt → executor → shared response/event bridge
```

`prepare_attempt()` receives the target wire payload, provider-qualified
`ModelRef`, stream mode, and immutable `AttemptRequestContext`. It returns a
`PreparedAttempt` with the provider URL, headers, and isolated payload. Provider
quirks such as model parameters, auth headers, URL paths, and body adjustments
belong here. Never mutate the source envelope or another attempt's data. Forward
only explicitly allowed client headers; do not forward the RM credential.

Response/SSE normalization belongs in the provider executor when it is specific
to that provider. `EndpointBinding.runtime_options` supplies typed deviations
understood by shared codecs, such as opaque Chat reasoning or per-event Responses
IDs. The core runtime factory does not identify Copilot or other providers by
name. Options apply only to the selected provider binding, not ingress codecs.
There is no arbitrary pre-IR mutation hook; add one only with a concrete need and
an explicit immutable-attempt contract.

## Transport policy

`routing/transport_policy.py` is the source of the shared default chains:
Responses → Chat; Messages → Responses → Chat; Chat; Gemini → Chat.
Unregistered protocols are never invented. Model capability negatives, stream
support, exact representability, and continuation affinity constrain candidates.

`TransportPolicy` has independent switches:

- `compatibility_transports`: append supported cross-protocol paths beyond the
  default chain, preserving clients of a provider with a different native API.
- `recover_retryable_errors`: permit another transport after a retryable failure;
  false for native plugins by default.
- `recover_request_rejections`: permit another binding after an option or
  unsupported-operation rejection; true by default and disabled by DeepSeek.

Copilot opts into compatibility and retryable-error recovery. Anthropic and
OpenAI-compatible providers opt into compatibility. DeepSeek uses default chains
but does not replay failed native attempts. Legacy bindings preserve their prior
compatibility/recovery behavior. Provider overrides of `transport_candidates`
and `transport_preferences` remain available for genuine transport restrictions;
they should not duplicate the standard conversion matrix.

None of these switches overrides exact-conversion checks, first-frame stream
commitment, or provider/model/binding affinity for `previous_response_id` and
opaque reasoning capsules. Inexact requests fail explicitly rather than losing
tools, reasoning, or continuation state. Advanced model RoutePlans remain
independent of translation and plugin registration.

## Resource endpoints

Provider-specific resources, such as DeepSeek Files, are `ProviderEndpoint`
declarations. A handler receives the request and the current provider instance.
The core mounts it with mandatory RM authentication, low-cardinality HTTP
metrics/request IDs, a declared wire error format, and a Router lease that lasts
through response-body completion, failure, or cancellation. It does not capture
an instance at route registration, so a config reload cannot direct new requests
to a retired provider.

Paths must be non-admin `/api/` paths, with complete single-segment path
parameters. Methods are explicit. Duplicate method/path pairs and parameter
shadowing are rejected during app construction, including conflicts with core
routes. Distinct methods may share a path and use their own declared error
format. Register stable aliases explicitly; do not select resource owners using
generation model priority.

Collision checks include nested router prefixes and routes hidden from OpenAPI,
including FastAPI versions that register included routers lazily. Preflight
metrics use the same effective path templates instead of resource IDs.

DeepSeek declares both `/api/providers/deepseek/v1/files` and the existing
`/api/openai/v1/files` compatibility alias, plus `/{file_id}`. Multipart bodies
stream to the provider without generation IR or model discovery. The provider
validates file IDs, replaces authentication, and filters returned headers.
Upstream errors retain their status, body, and allowed headers: the live DeepSeek
API returns HTTP 400 for a deleted file ID, which is not remapped to HTTP 404.
Files remain scoped to its configured credential; upload/delete are not replayed
to another provider. Other providers must choose distinct namespaces or resolve
alias ownership explicitly. Plugin code is trusted application code, not a
sandboxed extension.

Standard endpoints such as Anthropic `count_tokens` stay shared. Providers may
implement `count_tokens(protocol, payload, model=...)`; return `None` when no
exact native count is available so the existing estimator can run.

## Validation

Relevant offline suites include `test_provider_plugins.py`,
`test_transport_policy.py`, `test_openai_responses_binding.py`, and
`test_deepseek_files.py`. They verify real ASGI entry paths, isolated HTTP/SSE
execution, identity/cross-protocol conversion, auth, conflict detection,
OpenAPI routes, cancellation/reload lifecycle, and recovery/continuation gates.

Run the complete unit and controlled end-to-end suites:

```bash
uv run --frozen pytest tests/ -q
uv run --frozen pytest integration_tests/test_controlled_boundaries.py -q
```

The controlled suite runs a local subprocess and fake upstream against temporary
XDG state. It does not use personal credentials or make real provider requests.
Live-backend validation remains a separate, explicitly selected integration run.
