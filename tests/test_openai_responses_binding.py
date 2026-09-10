"""Native Responses stays wire-faithful through shared HTTP execution."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from router_maestro.auth.repository import CredentialRepository
from router_maestro.config import CustomProviderConfig, PrioritiesConfig, ProvidersConfig
from router_maestro.config.providers import CustomProviderOptions
from router_maestro.protocols import WireProtocol
from router_maestro.providers.base import ModelInfo, ProviderError, ProviderFailureKind
from router_maestro.providers.bindings import (
    OPENAI_COMPATIBLE_RESPONSES_BINDING,
    AttemptRequestContext,
)
from router_maestro.providers.custom_factory import create_custom_provider
from router_maestro.providers.http_executor import ProviderHttpClientPool
from router_maestro.providers.openai import OpenAIProvider
from router_maestro.providers.openai_compat import OpenAICompatibleProvider
from router_maestro.providers.registry import ProviderRegistry, default_provider_registry
from router_maestro.routing.model_ref import ModelRef
from router_maestro.routing.router import Router
from router_maestro.routing.transport_policy import TransportPolicy
from router_maestro.runtime.reasoning_capsule import ReasoningCapsuleCodec
from router_maestro.server.generation_pipeline import build_generation_pipeline


def _reply():
    return {
        "id": "resp_native",
        "object": "response",
        "status": "completed",
        "model": "model",
        "output": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [],
                "encrypted_content": "opaque-provider-state",
            }
        ],
        "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        "future_response": {"kept": True},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("official", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_responses_binding_keeps_items_unknown_fields_and_native_sse(official, stream):
    if official:
        provider = OpenAIProvider(base_url="https://example.invalid/v1")
        provider._get_headers = lambda: {"Authorization": "Bearer upstream-key"}
    else:
        provider = OpenAICompatibleProvider(
            "custom", "https://example.invalid/v1", "upstream-key", responses=True
        )
    captured = []
    reply = _reply()
    frames = [
        {
            "type": "response.created",
            "response": {
                "id": reply["id"],
                "model": "model",
                "status": "in_progress",
                "output": [],
            },
        },
        {"type": "response.completed", "response": reply},
    ]

    async def upstream(request):
        captured.append(
            (str(request.url), dict(request.headers), json.loads(await request.aread()))
        )
        if stream:
            content = "".join(
                f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n" for frame in frames
            )
            return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=reply)

    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    provider._http_client_pool = ProviderHttpClientPool(lambda: client)
    binding = next(
        binding
        for binding in provider.bindings()
        if binding.protocol is WireProtocol.OPENAI_RESPONSES
    )
    source = {
        "model": "public",
        "input": [
            {
                "type": "reasoning",
                "id": "rs_prior",
                "encrypted_content": "opaque-history",
                "summary": [],
            },
            {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "found"},
        ],
        "previous_response_id": "resp_previous",
        "future_request": {"kept": True},
    }
    original = deepcopy(source)
    assert binding.executor is not None
    attempt = await binding.prepare_attempt(
        model=ModelRef(provider.name, "model"), payload=source, stream=stream
    )
    try:
        if stream:
            assert [frame async for frame in binding.executor.execute_stream(attempt)] == frames
        else:
            assert await binding.executor.execute(attempt) == reply
        assert captured[0][0] == "https://example.invalid/v1/responses"
        assert captured[0][1]["authorization"] == "Bearer upstream-key"
        assert captured[0][2] == {**original, "model": "model", "stream": stream}
        assert "stream_options" not in captured[0][2]
        assert source == original
    finally:
        await provider.close()
    assert client.is_closed


@pytest.mark.asyncio
async def test_official_responses_ingress_selects_identity_without_chat_fallback(monkeypatch):
    monkeypatch.setattr("router_maestro.routing.router.load_providers_config", ProvidersConfig)
    registry = ProviderRegistry(
        plugin for plugin in default_provider_registry().plugins if plugin.id == "openai"
    )
    model_router = Router(PrioritiesConfig(), provider_registry=registry)
    provider = model_router.providers["openai"]
    assert isinstance(provider, OpenAIProvider)
    provider.is_authenticated = lambda: True
    provider._get_headers = lambda: {"Authorization": "Bearer upstream-key"}

    async def catalog():
        return [ModelInfo(id="model", name="Model", provider="openai")]

    provider.list_models = catalog
    requests = []

    async def upstream(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=_reply())

    provider._http_client_pool = ProviderHttpClientPool(
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    )
    pipeline = build_generation_pipeline(
        model_router,
        ReasoningCapsuleCodec(bytes([18]) * 32),
        WireProtocol.OPENAI_RESPONSES,
        {"model": "openai/model", "input": "hello"},
    )
    try:
        result = await pipeline.dispatcher.dispatch(model_router, pipeline.envelope)
        assert result.selection.plan.binding.id == OPENAI_COMPATIBLE_RESPONSES_BINDING
        assert pipeline.envelope.materialization_count == 0
        assert requests == ["/v1/responses"]
    finally:
        await model_router.close()


def test_custom_responses_is_explicit_and_credential_policy_is_unchanged():
    config = CustomProviderConfig(
        baseURL="https://custom.example/v1", options=CustomProviderOptions(responses=True)
    )
    provider = create_custom_provider(
        "custom",
        config,
        credential_repository=CredentialRepository(),
        environ={"CUSTOM_API_KEY": "test-key"},
    )
    assert provider is not None
    assert {binding.protocol for binding in provider.bindings()} == {
        WireProtocol.OPENAI_CHAT,
        WireProtocol.OPENAI_RESPONSES,
    }
    config.options.responses = False
    provider = create_custom_provider(
        "custom",
        config,
        credential_repository=CredentialRepository(),
        environ={"CUSTOM_API_KEY": "test-key"},
    )
    assert provider is not None
    assert [binding.protocol for binding in provider.bindings()] == [WireProtocol.OPENAI_CHAT]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status", [401, 429, 500])
async def test_native_responses_http_errors_keep_failure_classification(stream, status):
    provider = OpenAICompatibleProvider(
        "custom", "https://example.invalid/v1", "key", responses=True
    )
    calls = []

    async def upstream(request):
        calls.append(request.url.path)
        return httpx.Response(status, json={"error": {"message": "upstream failure"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    provider._http_client_pool = ProviderHttpClientPool(lambda: client)
    binding = provider.bindings()[1]
    assert binding.executor is not None
    attempt = await binding.prepare_attempt(
        model=ModelRef("custom", "model"), payload={"input": "hello"}, stream=stream
    )
    try:
        with pytest.raises(ProviderError) as raised:
            if stream:
                async for _ in binding.executor.execute_stream(attempt):
                    pass
            else:
                await binding.executor.execute(attempt)
        assert raised.value.upstream_status_code == status
        assert raised.value.retryable is (status in {429, 500})
        assert raised.value.provider == "custom"
        assert calls == ["/v1/responses"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_unadvertised_responses_cannot_be_reached_through_shared_executor():
    enabled = OpenAICompatibleProvider(
        "custom", "https://example.invalid/v1", "key", responses=True
    )
    disabled = OpenAICompatibleProvider("custom", "https://example.invalid/v1", "key")
    binding = enabled.bindings()[1]
    attempt = await binding.prepare_attempt(
        model=ModelRef("custom", "model"), payload={"input": "hello"}, stream=False
    )
    disabled_binding = disabled.bindings()[0]
    assert disabled_binding.executor is not None
    assert disabled_binding.dialect is not None
    with pytest.raises(ValueError, match="not enabled"):
        await disabled_binding.executor.execute(attempt)
    with pytest.raises(ValueError, match="not enabled"):
        await disabled_binding.dialect.prepare_attempt(
            binding_id=attempt.binding_id,
            protocol=attempt.protocol,
            model=attempt.model,
            payload=attempt.payload,
            stream=False,
            request_context=AttemptRequestContext(),
        )
    assert disabled._http_client_pool.client is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_previous_response_affinity_blocks_replay_even_with_recovery_enabled(
    monkeypatch, stream
):
    monkeypatch.setattr("router_maestro.routing.router.load_providers_config", ProvidersConfig)

    class RecoveryProvider(OpenAIProvider):
        @property
        def transport_policy(self):
            return TransportPolicy(compatibility_transports=True, recover_retryable_errors=True)

    plugin = next(plugin for plugin in default_provider_registry().plugins if plugin.id == "openai")
    model_router = Router(
        PrioritiesConfig(),
        provider_registry=ProviderRegistry((replace(plugin, factory=RecoveryProvider),)),
    )
    provider = model_router.providers["openai"]
    assert isinstance(provider, OpenAIProvider)
    provider.is_authenticated = lambda: True
    provider._get_headers = lambda: {"Authorization": "Bearer key"}

    async def catalog():
        return [ModelInfo(id="model", name="Model", provider="openai")]

    provider.list_models = catalog
    calls = []

    async def upstream(request):
        calls.append(request.url.path)
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    provider._http_client_pool = ProviderHttpClientPool(
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    )
    pipeline = build_generation_pipeline(
        model_router,
        ReasoningCapsuleCodec(bytes([72]) * 32),
        WireProtocol.OPENAI_RESPONSES,
        {
            "model": "openai/model",
            "input": "hello",
            "previous_response_id": "resp_previous",
            "stream": stream,
        },
    )
    try:
        with pytest.raises(ProviderError) as raised:
            if stream:
                await pipeline.dispatcher.dispatch_stream(model_router, pipeline.envelope)
            else:
                await pipeline.dispatcher.dispatch(model_router, pipeline.envelope)
        assert raised.value.kind is ProviderFailureKind.UPSTREAM_STATUS
        assert calls == ["/v1/responses"]
        assert pipeline.envelope.materialization_count == 0
    finally:
        await model_router.close()


def test_responses_opt_in_round_trips_without_changing_unrelated_options():
    original = {
        "providers": {
            "custom": {
                "baseURL": "https://example.invalid",
                "options": {"responses": True, "future": {"keep": 1}},
            }
        }
    }
    config = ProvidersConfig.model_validate(original)
    restored = ProvidersConfig.model_validate_json(config.model_dump_json())
    assert restored.providers["custom"].options.responses is True
    assert restored.providers["custom"].options.model_dump()["future"] == {"keep": 1}
