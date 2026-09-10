"""Plugin-only providers and endpoints exercise the real application boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from router_maestro.auth.discovery import ProviderAuthDefinition, ProviderAuthSource
from router_maestro.auth.storage import AuthType
from router_maestro.config import PrioritiesConfig, ProvidersConfig
from router_maestro.config.providers import CustomProviderOptions
from router_maestro.protocols import OpenAIResponsesRuntime, WireProtocol
from router_maestro.providers.base import BaseProvider, ModelInfo, ProviderError
from router_maestro.providers.bindings import (
    AttemptRequestContext,
    EndpointBinding,
    PreparedAttempt,
    ProtocolRuntimeOptions,
)
from router_maestro.providers.endpoints import ProviderEndpoint
from router_maestro.providers.registry import (
    ProviderPlugin,
    ProviderRegistry,
    default_provider_registry,
)
from router_maestro.routing.capabilities import Operation, ProviderCapabilities
from router_maestro.routing.model_ref import ModelRef
from router_maestro.routing.router import Router
from router_maestro.runtime.reasoning_capsule import ReasoningCapsuleCodec
from router_maestro.runtime.request_context import RequestContextMiddleware
from router_maestro.server.app import create_app
from router_maestro.server.dependencies import get_app_router
from router_maestro.server.generation_pipeline import build_generation_pipeline
from router_maestro.server.protocols.runtime_factory import ProtocolRuntimeFactory
from router_maestro.server.provider_endpoints import mount_provider_endpoints
from router_maestro.server.routes import anthropic as anthropic_route
from router_maestro.server.schemas.anthropic import AnthropicCountTokensRequest


class PluginOnlyProvider(BaseProvider):
    name = "example"

    def __init__(self) -> None:
        self.attempts: list[PreparedAttempt] = []
        self.close_count = 0
        self.count_calls: list[tuple[WireProtocol, str]] = []
        self.binding = EndpointBinding(
            id="example-responses",
            protocol=WireProtocol.OPENAI_RESPONSES,
            capabilities=ProviderCapabilities(
                operations=frozenset({Operation.RESPONSES, Operation.RESPONSES_STREAM})
            ),
            dialect=self,
            executor=self,
        )

    @property
    def id(self) -> str:
        return "example-dialect"

    def bindings(self) -> tuple[EndpointBinding, ...]:
        return (self.binding,)

    def is_authenticated(self) -> bool:
        return True

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="model", name="Model", provider=self.name)]

    async def close(self) -> None:
        self.close_count += 1

    async def count_tokens(self, protocol, payload, *, model):
        self.count_calls.append((protocol, model))
        return 11

    async def prepare_attempt(
        self,
        *,
        binding_id: str,
        protocol: WireProtocol,
        model: ModelRef,
        payload,
        stream: bool,
        request_context: AttemptRequestContext,
    ) -> PreparedAttempt:
        return PreparedAttempt(
            binding_id=binding_id,
            protocol=protocol,
            model=model,
            url="https://example.invalid/responses",
            payload={**payload, "model": model.upstream_id, "provider_option": True},
            headers={"x-example": "plugin-owned"},
            stream=stream,
        )

    async def execute(self, attempt: PreparedAttempt):
        self.attempts.append(attempt)
        return {
            "id": "resp_plugin",
            "object": "response",
            "status": "completed",
            "model": attempt.model.upstream_id,
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            "output": [
                {
                    "id": "msg_plugin",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "plugin reply"}],
                }
            ],
        }

    async def execute_stream(self, attempt: PreparedAttempt):
        self.attempts.append(attempt)
        yield {
            "type": "response.created",
            "response": {
                "id": "resp_plugin",
                "model": "model",
                "status": "in_progress",
                "output": [],
            },
        }
        yield {"type": "response.completed", "response": await self.execute(attempt)}


async def _echo(provider: BaseProvider, request: Request) -> Response:
    return JSONResponse({"provider": provider.name, "file": request.path_params.get("file_id")})


def _plugin(
    *,
    endpoints=None,
    factory: Callable[[], BaseProvider] = PluginOnlyProvider,
    provider_id="example",
) -> ProviderPlugin:
    return ProviderPlugin(
        id=provider_id,
        factory=factory,
        auth=ProviderAuthDefinition(
            provider=provider_id,
            display_name="Example plugin",
            auth_type=AuthType.API_KEY,
            credential_required=True,
            source=ProviderAuthSource.BUILTIN,
        ),
        endpoints=(
            ProviderEndpoint(
                name="files",
                path="/api/providers/example/files/{file_id}",
                methods=frozenset({"GET"}),
                handler=_echo,
            ),
        )
        if endpoints is None
        else endpoints,
    )


@pytest.fixture
def isolated_config(monkeypatch):
    monkeypatch.setattr("router_maestro.routing.router.load_providers_config", ProvidersConfig)
    monkeypatch.setenv("ROUTER_MAESTRO_API_KEY", "test-router-key")
    return PrioritiesConfig(priorities=["example/model"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ingress", [WireProtocol.OPENAI_RESPONSES, WireProtocol.ANTHROPIC_MESSAGES]
)
async def test_binding_only_plugin_runs_shared_generation_and_request_hook(
    isolated_config, ingress
):
    model_router = Router(isolated_config, provider_registry=ProviderRegistry((_plugin(),)))
    provider = cast(PluginOnlyProvider, model_router.providers["example"])
    source = {"model": "example/model", "input": "hello", "future": {"kept": True}}
    if ingress is WireProtocol.ANTHROPIC_MESSAGES:
        source = {
            "model": "example/model",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hello"}],
        }
    pipeline = build_generation_pipeline(
        model_router, ReasoningCapsuleCodec(bytes([31]) * 32), ingress, source
    )
    try:
        result = await pipeline.dispatcher.dispatch(model_router, pipeline.envelope)
        reply = await pipeline.responses.encode_result(result, pipeline.envelope.runtime)
        assert provider.attempts[0].payload["provider_option"] is True
        assert provider.attempts[0].headers == {"x-example": "plugin-owned"}
        assert "provider_option" not in source
        assert reply["model"] == "example/model"
        assert pipeline.envelope.materialization_count == int(
            ingress is not WireProtocol.OPENAI_RESPONSES
        )
        if ingress is WireProtocol.OPENAI_RESPONSES:
            assert provider.attempts[0].payload["future"] == {"kept": True}
    finally:
        await model_router.close()
    assert provider.close_count == 1


@pytest.mark.asyncio
async def test_plugin_endpoint_auth_catalog_errors_and_shutdown(isolated_config):
    providers = []

    def factory():
        provider = PluginOnlyProvider()
        providers.append(provider)
        return provider

    app = create_app(provider_registry=ProviderRegistry((_plugin(factory=factory),)))
    owner = app.state.router_owner
    await owner.start(isolated_config)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            denied = await client.get("/api/providers/example/files/id")
            assert denied.status_code == 401
            assert denied.json()["error"]["type"] == "authentication_error"
            headers = {"Authorization": "Bearer test-router-key"}
            reply = await client.get("/api/providers/example/files/id", headers=headers)
            assert reply.json() == {"provider": "example", "file": "id"}
            assert reply.headers["x-request-id"]
            discovery = await client.get("/api/admin/auth/providers", headers=headers)
            assert discovery.json()["providers"][0]["provider"] == "example"
            assert not any("deepseek" in getattr(route, "path", "") for route in app.routes)
    finally:
        await owner.close()
    assert providers[0].close_count == 1


@pytest.mark.asyncio
async def test_plugin_endpoint_keeps_old_generation_alive_until_stream_finishes(isolated_config):
    started = asyncio.Event()
    release = asyncio.Event()
    providers = []

    def factory():
        provider = PluginOnlyProvider()
        providers.append(provider)
        return provider

    async def stream(provider, request):
        async def chunks():
            started.set()
            await release.wait()
            assert provider.close_count == 0
            yield b"completed"

        return StreamingResponse(chunks())

    endpoint = ProviderEndpoint(
        "stream", "/api/providers/example/stream", frozenset({"GET"}), stream
    )
    app = create_app(
        provider_registry=ProviderRegistry((_plugin(factory=factory, endpoints=(endpoint,)),))
    )
    owner = app.state.router_owner
    await owner.start(isolated_config)
    pending = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            pending = asyncio.create_task(
                client.get(
                    "/api/providers/example/stream",
                    headers={"Authorization": "Bearer test-router-key"},
                )
            )
            await asyncio.wait_for(started.wait(), timeout=2)
            await owner.rebuild(isolated_config)
            assert len(providers) == 2
            assert providers[0].close_count == 0
            release.set()
            response = await asyncio.wait_for(pending, timeout=2)
            assert response.content == b"completed"
            assert providers[0].close_count == 1
            assert providers[1].close_count == 0
            response = await client.get(
                "/api/providers/example/stream", headers={"Authorization": "Bearer test-router-key"}
            )
            assert response.status_code == 200
    finally:
        release.set()
        if pending is not None:
            await pending
        await owner.close()
    assert [provider.close_count for provider in providers] == [1, 1]


@pytest.mark.parametrize("path", ["/api/openai/v1/responses", "/api/openai/v1/{resource}"])
def test_plugin_routes_cannot_shadow_core_routes(path):
    endpoint = ProviderEndpoint("collision", path, frozenset({"POST"}), _echo)
    with pytest.raises(ValueError, match="conflicts"):
        create_app(provider_registry=ProviderRegistry((_plugin(endpoints=(endpoint,)),)))


@pytest.mark.parametrize(
    ("path", "method", "conflicts"),
    [
        ("/api/providers/example/v1/files/{resource_id}", "GET", True),
        ("/api/providers/example/v1/files/static", "GET", True),
        ("/api/providers/example/v1/files/{resource_id}", "DELETE", False),
        ("/api/providers/example/v1/siblings/{resource_id}", "GET", False),
        ("/api/providers/other/v1/files/{resource_id}", "GET", False),
    ],
)
def test_plugin_collisions_respect_nested_router_prefixes_and_methods(path, method, conflicts):
    app = FastAPI()
    parent = APIRouter()
    child = APIRouter()

    @child.get("/files/{file_id}", include_in_schema=False)
    async def core_resource(file_id: str):
        return {"id": file_id}

    parent.include_router(child, prefix="/example/v1")
    app.include_router(parent, prefix="/api/providers")
    endpoint = ProviderEndpoint("nested", path, frozenset({method}), _echo)
    registry = ProviderRegistry((_plugin(endpoints=(endpoint,)),))
    if conflicts:
        with pytest.raises(ValueError, match="conflicts"):
            mount_provider_endpoints(app, registry)
    else:
        mount_provider_endpoints(app, registry)


def test_duplicate_aliases_provider_ids_and_endpoint_names_fail_closed():
    plugin = _plugin()
    with pytest.raises(ValueError, match="unique"):
        ProviderRegistry((plugin, plugin))
    with pytest.raises(ValueError, match="duplicate endpoint names"):
        replace(plugin, endpoints=(*plugin.endpoints, *plugin.endpoints))
    other = _plugin(provider_id="other")
    with pytest.raises(ValueError, match="conflicts"):
        create_app(provider_registry=ProviderRegistry((plugin, other)))


@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/api/admin/auth",
        "/api/providers/{path:path}",
        "/api/providers/example/../files",
        "/api/providers/example/file-{id}",
    ],
)
def test_endpoint_declarations_reject_unsafe_or_ambiguous_paths(path):
    with pytest.raises(ValueError):
        ProviderEndpoint("bad", path, frozenset({"GET"}), _echo)


def test_registry_extension_is_explicit_and_does_not_mutate_defaults():
    baseline = default_provider_registry()
    extended = baseline.with_plugins(_plugin())
    assert "example" not in baseline.provider_ids
    assert "example" in extended.provider_ids
    assert "example" not in default_provider_registry().provider_ids
    assert len(extended.plugins) == len(baseline.plugins) + 1


@pytest.mark.asyncio
async def test_different_methods_on_one_path_use_their_own_error_protocol(isolated_config):
    async def fail(provider, request):
        raise ProviderError("Invalid resource", status_code=400)

    path = "/api/providers/example/shared"
    endpoints = (
        ProviderEndpoint("read", path, frozenset({"GET"}), fail),
        ProviderEndpoint("write", path, frozenset({"POST"}), fail, WireProtocol.GEMINI),
    )
    app = create_app(provider_registry=ProviderRegistry((_plugin(endpoints=endpoints),)))
    app.dependency_overrides[get_app_router] = lambda: SimpleNamespace(
        providers={"example": PluginOnlyProvider()}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        headers = {"Authorization": "Bearer test-router-key"}
        read = await client.get(path, headers=headers)
        write = await client.post(path, headers=headers)
        denied = await client.post(path)
    assert "type" in read.json()["error"]
    assert write.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert denied.json()["error"]["status"] == "UNAUTHENTICATED"


@pytest.mark.asyncio
async def test_missing_plugin_instance_fails_without_calling_handler(isolated_config):
    calls = []

    async def handle(provider, request):
        calls.append(provider)
        return Response()

    endpoint = ProviderEndpoint(
        "missing", "/api/providers/example/missing", frozenset({"GET"}), handle
    )
    app = create_app(provider_registry=ProviderRegistry((_plugin(endpoints=(endpoint,)),)))
    app.dependency_overrides[get_app_router] = lambda: SimpleNamespace(providers={})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(
            endpoint.path, headers={"Authorization": "Bearer test-router-key"}
        )
    assert response.status_code == 503
    assert calls == []


@pytest.mark.asyncio
async def test_cancelled_plugin_stream_releases_retired_generation(isolated_config):
    started = asyncio.Event()
    providers = []

    def factory():
        provider = PluginOnlyProvider()
        providers.append(provider)
        return provider

    async def handle(provider, request):
        async def chunks():
            started.set()
            await asyncio.Event().wait()
            yield b"unreachable"

        return StreamingResponse(chunks())

    endpoint = ProviderEndpoint(
        "cancel", "/api/providers/example/cancel", frozenset({"GET"}), handle
    )
    app = create_app(
        provider_registry=ProviderRegistry((_plugin(factory=factory, endpoints=(endpoint,)),))
    )
    owner = app.state.router_owner
    await owner.start(isolated_config)
    pending = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            pending = asyncio.create_task(
                client.get(endpoint.path, headers={"Authorization": "Bearer test-router-key"})
            )
            await asyncio.wait_for(started.wait(), 2)
            await owner.rebuild(isolated_config)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert providers[0].close_count == 1
            assert providers[1].close_count == 0
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await asyncio.wait_for(owner.close(), 2)
    assert [provider.close_count for provider in providers] == [1, 1]


@pytest.mark.asyncio
async def test_plugin_failure_releases_lease_and_does_not_leak_error_details(isolated_config):
    async def fail(provider, request):
        raise RuntimeError("private-upstream-credential")

    endpoint = ProviderEndpoint(
        "failure", "/api/providers/example/failure", frozenset({"GET"}), fail
    )
    app = create_app(provider_registry=ProviderRegistry((_plugin(endpoints=(endpoint,)),)))
    owner = app.state.router_owner
    await owner.start(isolated_config)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            response = await client.get(
                endpoint.path, headers={"Authorization": "Bearer test-router-key"}
            )
        assert response.status_code == 500
        assert "private-upstream-credential" not in response.text
    finally:
        await asyncio.wait_for(owner.close(), 2)


def test_plugin_and_configured_provider_ids_cannot_collide_before_factories_run(
    isolated_config, monkeypatch
):
    from router_maestro.config import CustomProviderConfig

    calls = []
    config = ProvidersConfig(
        providers={"EXAMPLE": CustomProviderConfig(baseURL="https://example.invalid")}
    )
    monkeypatch.setattr("router_maestro.routing.router.load_providers_config", lambda: config)

    def factory():
        calls.append(True)
        return PluginOnlyProvider()

    with pytest.raises(ValueError, match="conflicts"):
        Router(isolated_config, provider_registry=ProviderRegistry((_plugin(factory=factory),)))
    assert calls == []


def test_configured_factories_are_extensible_and_keep_configuration_and_credentials(
    isolated_config, monkeypatch
):
    from router_maestro.config import CustomProviderConfig

    calls = []
    config = ProvidersConfig(
        providers={
            "example": CustomProviderConfig(
                type="example-type",
                baseURL="https://example.invalid",
                options=CustomProviderOptions.model_validate({"native_option": 1}),
            )
        }
    )

    def factory(provider_name, provider_config, *, credential_repository):
        calls.append((provider_name, provider_config, credential_repository))
        return PluginOnlyProvider()

    registry = ProviderRegistry().with_configured_factory("example-type", factory)
    monkeypatch.setattr("router_maestro.routing.router.load_providers_config", lambda: config)
    router = Router(isolated_config, provider_registry=registry)
    assert set(router.providers) == {"example"}
    assert calls[0][0] == "example"
    assert calls[0][1].options.native_option == 1
    assert calls[0][2] is not None
    with pytest.raises(ValueError, match="unique"):
        registry.with_configured_factory("example-type", factory)


def test_plugin_factory_and_auth_metadata_must_match_provider_identity(isolated_config):
    from router_maestro.auth.repository import CredentialRepository
    from router_maestro.config import CustomProviderConfig

    with pytest.raises(ValueError, match="authentication metadata"):
        replace(_plugin(), id="mismatch")
    with pytest.raises(ValueError, match="different provider ID"):
        Router(isolated_config, provider_registry=ProviderRegistry((_plugin(provider_id="other"),)))
    registry = ProviderRegistry(
        configured_factories={"native": lambda *args, **kwargs: PluginOnlyProvider()}
    )
    with pytest.raises(ValueError, match="different provider ID"):
        registry.create_configured(
            "other",
            CustomProviderConfig(type="native", baseURL="https://example.invalid"),
            credential_repository=CredentialRepository(),
        )


@pytest.mark.parametrize("methods", [frozenset(), frozenset({"get"}), frozenset({"TRACE"})])
def test_extension_methods_must_be_explicit_supported_verbs(methods):
    with pytest.raises(ValueError, match="methods"):
        ProviderEndpoint("invalid", "/api/providers/example/method", methods, _echo)


def test_plugin_openapi_operations_are_unique_and_preserve_file_aliases():
    app = create_app()
    schema = app.openapi()
    ids = [
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    assert len(ids) == len(set(ids))
    for prefix in ("/api/openai/v1/files", "/api/providers/deepseek/v1/files"):
        assert set(schema["paths"][prefix]) == {"get", "post"}
        assert set(schema["paths"][prefix + "/{file_id}"]) == {"get", "delete"}


def test_plugin_runtime_options_apply_without_provider_name_checks():
    provider = PluginOnlyProvider()
    provider.binding = replace(
        provider.binding,
        runtime_options=ProtocolRuntimeOptions(
            allow_per_event_response_ids=True, defer_intermediate_item_ids=True
        ),
    )
    router = cast(Router, SimpleNamespace(providers={provider.name: provider}))
    factory = ProtocolRuntimeFactory.for_router(router, ReasoningCapsuleCodec(bytes([32]) * 32))
    runtime = factory._build(
        WireProtocol.OPENAI_RESPONSES,
        model="model",
        stream=False,
        provider=provider.name,
        binding=provider.binding.id,
    )
    assert isinstance(runtime, OpenAIResponsesRuntime)
    assert runtime.allow_per_event_response_ids
    assert runtime.defer_intermediate_item_ids
    ingress = factory.ingress(WireProtocol.OPENAI_RESPONSES)
    assert isinstance(ingress, OpenAIResponsesRuntime)
    assert not ingress.allow_per_event_response_ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ingress", [WireProtocol.OPENAI_RESPONSES, WireProtocol.ANTHROPIC_MESSAGES]
)
@pytest.mark.parametrize("stream", [False, True])
async def test_registered_provider_runs_real_http_generation_routes(
    isolated_config, ingress, stream
):
    app = create_app(provider_registry=ProviderRegistry((_plugin(),)))
    app.state.reasoning_capsule_codec = ReasoningCapsuleCodec(bytes([91]) * 32)
    snapshot = SimpleNamespace(config=isolated_config, revision="test-plugin-revision")
    app.state.runtime_config_repository = SimpleNamespace(read=lambda: snapshot)
    owner = app.state.router_owner
    await owner.start(snapshot)
    if ingress is WireProtocol.OPENAI_RESPONSES:
        path = "/api/openai/v1/responses"
        payload = {"model": "example/model", "input": "hello", "stream": stream}
    else:
        path = "/api/anthropic/v1/messages"
        payload = {
            "model": "example/model",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hello"}],
            "stream": stream,
        }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                path, json=payload, headers={"Authorization": "Bearer test-router-key"}
            )
        assert response.status_code == 200, response.text
        if stream:
            assert response.headers["content-type"].startswith("text/event-stream")
            assert (
                "response.completed" in response.text
                if ingress is WireProtocol.OPENAI_RESPONSES
                else "message_stop" in response.text
            )
            assert "event: error" not in response.text
        else:
            assert response.json()["model"] == "example/model"
            assert "plugin reply" in response.text
    finally:
        await asyncio.wait_for(owner.close(), 2)


@pytest.mark.asyncio
async def test_request_context_setup_failure_releases_acquired_router_lease(monkeypatch):
    lease = SimpleNamespace(release=AsyncMock(), config_snapshot=None)
    owner = SimpleNamespace(start=AsyncMock(), acquire=AsyncMock(return_value=lease))
    state = SimpleNamespace(
        router_owner=owner, runtime_config_repository=SimpleNamespace(read=lambda: None)
    )

    def fail(**kwargs):
        raise RuntimeError("request setup failed")

    monkeypatch.setattr("router_maestro.runtime.request_context.RequestContext.create", fail)
    app = AsyncMock()
    middleware = RequestContextMiddleware(app)
    with pytest.raises(RuntimeError, match="request setup failed"):
        await middleware(
            {
                "type": "http",
                "path": "/api/openai/v1/responses",
                "app": SimpleNamespace(state=state),
            },
            AsyncMock(),
            AsyncMock(),
        )
    lease.release.assert_awaited_once()
    app.assert_not_awaited()


@pytest.mark.asyncio
async def test_standard_token_count_endpoint_uses_arbitrary_provider_capability(monkeypatch):
    provider = PluginOnlyProvider()

    async def resolve(_model):
        return provider.name

    monkeypatch.setattr(anthropic_route, "_resolve_provider_name", resolve)
    monkeypatch.setattr(
        anthropic_route, "get_router", lambda: SimpleNamespace(providers={provider.name: provider})
    )
    result = await anthropic_route.count_tokens(
        AnthropicCountTokensRequest(model="example/model", messages=[])
    )
    assert result == {"input_tokens": 11}
    assert provider.count_calls == [(WireProtocol.ANTHROPIC_MESSAGES, "model")]


@pytest.mark.asyncio
async def test_plugin_extension_uses_declared_error_protocol(isolated_config):
    async def fail(provider, request):
        raise ProviderError("Invalid resource", status_code=400)

    endpoint = ProviderEndpoint(
        "error",
        "/api/providers/example/error",
        frozenset({"GET"}),
        fail,
        error_protocol=WireProtocol.GEMINI,
    )
    app = create_app(provider_registry=ProviderRegistry((_plugin(endpoints=(endpoint,)),)))
    app.dependency_overrides[get_app_router] = lambda: SimpleNamespace(
        providers={"example": PluginOnlyProvider()}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(
            endpoint.path, headers={"Authorization": "Bearer test-router-key"}
        )
    assert response.status_code == 400
    assert response.json()["error"]["status"] == "INVALID_ARGUMENT"
