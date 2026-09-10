"""Contracts for the built-in DeepSeek multi-protocol provider."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from router_maestro.auth.storage import ApiKeyCredential
from router_maestro.protocols import ConversionMode, RequestManifest, WireProtocol
from router_maestro.providers.base import (
    ModelInfo,
    ProviderError,
    ProviderFailureKind,
    ProviderFailureSignal,
)
from router_maestro.providers.bindings import AttemptRequestContext
from router_maestro.providers.deepseek import (
    DEEPSEEK_ANTHROPIC_MESSAGES_BINDING,
    DEEPSEEK_OPENAI_CHAT_BINDING,
    DEEPSEEK_OPENAI_RESPONSES_BINDING,
    DeepSeekProvider,
)
from router_maestro.providers.handler import ProviderHandler
from router_maestro.providers.http_executor import ProviderHttpClientPool
from router_maestro.routing.capabilities import Feature, Operation
from router_maestro.routing.generation_plan import GenerationCandidate
from router_maestro.routing.model_ref import ModelRef
from router_maestro.routing.router import Router
from router_maestro.server.protocols.errors import protocol_error_response
from router_maestro.server.routes import anthropic as anthropic_route
from router_maestro.server.schemas.anthropic import (
    AnthropicCountTokensRequest,
    AnthropicUserMessage,
)


def _provider() -> DeepSeekProvider:
    provider = DeepSeekProvider(base_url="https://deepseek.example/")
    provider.auth_manager.get_credential = lambda _name: ApiKeyCredential(  # type: ignore[method-assign]
        key="upstream-secret"
    )
    return provider


def _candidate(provider: DeepSeekProvider, model_id: str = "deepseek-flash"):
    info = ModelInfo(id=model_id, name=model_id, provider=provider.name)
    return GenerationCandidate(
        model=ModelRef(provider.name, model_id),
        provider=provider,
        info=info,
    )


async def _documented_models():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://deepseek.example/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "deepseek-flash"},
                    {"id": "deepseek-v4-pro"},
                ],
            },
            request=request,
        )

    provider = _provider()
    try:
        with patch(
            "httpx.AsyncClient",
            return_value=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ):
            return await provider.list_models()
    finally:
        await provider.close()


def _binding(provider: DeepSeekProvider, protocol: WireProtocol):
    return next(binding for binding in provider.bindings() if binding.protocol is protocol)


def test_deepseek_provider_uses_official_base_and_requires_its_own_credential() -> None:
    provider = DeepSeekProvider()
    provider.auth_manager.get_credential = lambda _name: None  # type: ignore[method-assign]

    assert provider.name == "deepseek"
    assert provider.base_url == "https://api.deepseek.com"
    assert provider.is_authenticated() is False
    with pytest.raises(ProviderError) as raised:
        provider._get_headers()
    assert raised.value.kind is ProviderFailureKind.AUTHENTICATION
    assert raised.value.provider == "deepseek"


def test_deepseek_bindings_are_protocol_native_and_gemini_is_chat_only() -> None:
    provider = _provider()
    bindings = provider.bindings()

    assert bindings is provider.bindings()
    assert [(binding.id, binding.protocol, binding.is_legacy) for binding in bindings] == [
        (DEEPSEEK_ANTHROPIC_MESSAGES_BINDING, WireProtocol.ANTHROPIC_MESSAGES, False),
        (DEEPSEEK_OPENAI_CHAT_BINDING, WireProtocol.OPENAI_CHAT, False),
        (DEEPSEEK_OPENAI_RESPONSES_BINDING, WireProtocol.OPENAI_RESPONSES, False),
    ]
    assert provider.transport_candidates(WireProtocol.ANTHROPIC_MESSAGES) == (
        DEEPSEEK_ANTHROPIC_MESSAGES_BINDING,
        DEEPSEEK_OPENAI_CHAT_BINDING,
        DEEPSEEK_OPENAI_RESPONSES_BINDING,
    )
    assert provider.transport_candidates(WireProtocol.OPENAI_CHAT) == (
        DEEPSEEK_OPENAI_CHAT_BINDING,
    )
    assert provider.transport_candidates(WireProtocol.OPENAI_RESPONSES) == (
        DEEPSEEK_OPENAI_CHAT_BINDING,
        DEEPSEEK_OPENAI_RESPONSES_BINDING,
    )
    assert provider.transport_candidates(WireProtocol.GEMINI) == (DEEPSEEK_OPENAI_CHAT_BINDING,)


@pytest.mark.parametrize(
    ("ingress", "binding_id", "mode"),
    [
        (
            WireProtocol.ANTHROPIC_MESSAGES,
            DEEPSEEK_ANTHROPIC_MESSAGES_BINDING,
            ConversionMode.IDENTITY,
        ),
        (WireProtocol.OPENAI_CHAT, DEEPSEEK_OPENAI_CHAT_BINDING, ConversionMode.IDENTITY),
        (
            WireProtocol.OPENAI_RESPONSES,
            DEEPSEEK_OPENAI_RESPONSES_BINDING,
            ConversionMode.IDENTITY,
        ),
        (WireProtocol.GEMINI, DEEPSEEK_OPENAI_CHAT_BINDING, ConversionMode.SEMANTIC_IR),
    ],
)
def test_deepseek_handler_prefers_native_transport_per_ingress(
    ingress: WireProtocol,
    binding_id: str,
    mode: ConversionMode,
) -> None:
    provider = _provider()

    plans = ProviderHandler(provider).bindings_for(
        _candidate(provider),
        ingress,
        RequestManifest(protocol=ingress),
    )

    assert plans[0].binding.id == binding_id
    assert plans[0].conversion_mode is mode


@pytest.mark.asyncio
async def test_deepseek_chat_identity_preserves_dsh_body_and_safe_attribution_headers() -> None:
    provider = _provider()
    source = {
        "model": "deepseek-flash",
        "messages": [
            {"role": "assistant", "content": "", "reasoning_content": "plan"},
            {"role": "user", "content": "continue"},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
        "future_deepseek_extension": {"kept": True},
    }
    original = deepcopy(source)

    attempt = await _binding(provider, WireProtocol.OPENAI_CHAT).prepare_attempt(
        model=ModelRef("deepseek", "deepseek-flash"),
        payload=source,
        stream=True,
        request_context=AttemptRequestContext(
            path="/api/openai/v1/chat/completions",
            headers={
                "Authorization": "Bearer downstream-router-key",
                "X-DeepSeek-Harness-User-Id": "anonymous-id",
                "X-DeepSeek-Harness-Session-Id": "session-id",
                "X-DeepSeek-Harness-Compact": "1",
                "User-Agent": "DeepSeekHarness/test",
            },
            conversion_mode=ConversionMode.IDENTITY,
        ),
    )

    assert source == original
    assert attempt.url == "https://deepseek.example/chat/completions"
    assert dict(attempt.payload) == original
    assert attempt.headers["Authorization"] == "Bearer upstream-secret"
    assert attempt.headers["x-deepseek-harness-user-id"] == "anonymous-id"
    assert attempt.headers["x-deepseek-harness-session-id"] == "session-id"
    assert attempt.headers["x-deepseek-harness-compact"] == "1"
    assert attempt.headers["user-agent"] == "DeepSeekHarness/test"
    assert "downstream-router-key" not in repr(dict(attempt.headers))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("protocol", "expected_url", "expected_auth_header"),
    [
        (
            WireProtocol.OPENAI_RESPONSES,
            "https://deepseek.example/responses",
            "Authorization",
        ),
        (
            WireProtocol.ANTHROPIC_MESSAGES,
            "https://deepseek.example/anthropic/v1/messages",
            "x-api-key",
        ),
    ],
)
async def test_deepseek_native_dialects_preserve_unknown_fields_and_rewrite_only_route_facts(
    protocol: WireProtocol,
    expected_url: str,
    expected_auth_header: str,
) -> None:
    provider = _provider()
    source = {
        "model": "deepseek/deepseek-v4-pro",
        "stream": False,
        "future_wire_field": {"kept": True},
    }

    attempt = await _binding(provider, protocol).prepare_attempt(
        model=ModelRef("deepseek", "deepseek-v4-pro"),
        payload=source,
        stream=True,
        request_context=AttemptRequestContext(
            headers={
                "Authorization": "Bearer downstream-router-key",
                "X-Api-Key": "downstream-router-key",
                "Anthropic-Version": "2023-06-01",
                "Anthropic-Beta": "future-beta",
            },
            conversion_mode=ConversionMode.IDENTITY,
        ),
    )

    assert attempt.url == expected_url
    assert dict(attempt.payload) == {
        **source,
        "model": "deepseek-v4-pro",
        "stream": True,
    }
    assert attempt.payload["future_wire_field"] == {"kept": True}
    assert attempt.headers[expected_auth_header] in {
        "Bearer upstream-secret",
        "upstream-secret",
    }
    assert "downstream-router-key" not in repr(dict(attempt.headers))
    if protocol is WireProtocol.ANTHROPIC_MESSAGES:
        assert attempt.headers["anthropic-version"] == "2023-06-01"
        assert attempt.headers["anthropic-beta"] == "future-beta"


@pytest.mark.asyncio
async def test_deepseek_gemini_chat_conversion_removes_budget_and_maps_minimal_effort() -> None:
    provider = _provider()
    source = {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "hello"}],
        "thinking": {"type": "enabled", "budget_tokens": 512},
        "reasoning_effort": "minimal",
    }

    attempt = await _binding(provider, WireProtocol.OPENAI_CHAT).prepare_attempt(
        model=ModelRef("deepseek", "deepseek-flash"),
        payload=source,
        stream=False,
        request_context=AttemptRequestContext(conversion_mode=ConversionMode.SEMANTIC_IR),
    )

    assert attempt.payload["thinking"] == {"type": "enabled"}
    assert attempt.payload["reasoning_effort"] == "low"
    assert source["thinking"]["budget_tokens"] == 512


@pytest.mark.asyncio
async def test_deepseek_live_catalog_gets_documented_capabilities() -> None:
    models = await _documented_models()

    assert [model.id for model in models] == [
        "deepseek-flash",
        "deepseek-v4-pro",
    ]
    assert [model.name for model in models] == ["DeepSeek-V4.1-Flash", "DeepSeek-V4-Pro"]
    for model in models:
        assert model.provider == "deepseek"
        assert model.max_context_window_tokens == 1_000_000
        assert model.max_output_tokens == 384_000
        assert model.supports_thinking is True
        assert model.reasoning_effort_values == [
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ]
        assert model.operation_capabilities == {operation.value: True for operation in Operation}
        assert model.feature_capabilities[Feature.TOOLS.value] is True
        assert model.feature_capabilities[Feature.REASONING.value] is True
        assert Feature.PARALLEL_TOOLS.value not in model.feature_capabilities
        assert Feature.STRUCTURED_OUTPUT.value not in model.feature_capabilities
        assert model.transport_capabilities == {
            WireProtocol.ANTHROPIC_MESSAGES.value: True,
            WireProtocol.OPENAI_CHAT.value: True,
            WireProtocol.OPENAI_RESPONSES.value: True,
        }
    assert models[0].supports_vision is True
    assert models[0].feature_capabilities[Feature.VISION.value] is True
    assert models[0].feature_capabilities[Feature.FILES.value] is True
    assert models[1].supports_vision is False
    assert models[1].feature_capabilities[Feature.VISION.value] is False
    assert models[1].feature_capabilities[Feature.FILES.value] is False


@pytest.mark.asyncio
async def test_deepseek_catalog_falls_back_to_documented_models_on_transport_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    provider = _provider()
    with patch(
        "httpx.AsyncClient",
        return_value=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ):
        models = await provider.list_models()

    assert [model.id for model in models] == [
        "deepseek-flash",
        "deepseek-v4-pro",
    ]


def test_deepseek_current_and_retired_model_aliases_are_provider_owned() -> None:
    provider = _provider()

    assert provider.model_aliases() == {
        "deepseek-flash": "deepseek-flash",
        "deepseek-v4-pro": "deepseek-v4-pro",
        "deepseek-v4-flash": "deepseek-flash",
        "deepseek-v4-flash-vision-exp": "deepseek-flash",
        "deepseek/deepseek-v4-flash": "deepseek-flash",
        "deepseek/deepseek-v4-flash-vision-exp": "deepseek-flash",
    }


@pytest.mark.parametrize(
    "model_id",
    [
        "deepseek-flash",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-flash-vision-exp",
    ],
)
def test_deepseek_flash_aliases_normalize_to_canonical_model(model_id: str) -> None:
    router = Router.__new__(Router)
    router.providers = {"deepseek": _provider()}
    router._model_aliases = None

    assert router._normalize_model_alias(model_id) == "deepseek/deepseek-flash"


def test_deepseek_current_qualified_model_is_not_rewritten() -> None:
    router = Router.__new__(Router)
    router.providers = {"deepseek": _provider()}
    router._model_aliases = None

    assert router._normalize_model_alias("deepseek/deepseek-flash") == ("deepseek/deepseek-flash")


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (
            400,
            b'{"error":{"message":"This model\'s maximum context length is 1048576 tokens. '
            b"However, you requested 1048793 tokens (792793 in the messages, 256000 in the "
            b'completion). Please reduce the length of the messages or completion.",'
            b'"type":"invalid_request_error","code":"invalid_request_error"}}',
            ProviderFailureSignal.CONTEXT_WINDOW_EXCEEDED,
        ),
        (
            400,
            b'{"error":{"message":"ordinary bad request",'
            b'"type":"invalid_request_error","code":"invalid_request_error"}}',
            None,
        ),
        (
            400,
            b'{"error":{"message":"This model\'s maximum context length is 10 tokens. '
            b"However, you requested 11 tokens (9 in the messages, 2 in the completion). "
            b'Please reduce the length of the messages or completion.",'
            b'"type":"other","code":"invalid_request_error"}}',
            None,
        ),
        (
            400,
            b'{"error":{"message":"This model\'s maximum context length is 10 tokens. '
            b"However, you requested 9 tokens (7 in the messages, 2 in the completion). "
            b'Please reduce the length of the messages or completion.",'
            b'"type":"invalid_request_error","code":"invalid_request_error"}}',
            None,
        ),
        (
            400,
            b'{"error":{"message":"This model\'s maximum context length is 10 tokens. '
            b"However, you requested 12 tokens (9 in the messages, 2 in the completion). "
            b'Please reduce the length of the messages or completion.",'
            b'"type":"invalid_request_error","code":"invalid_request_error"}}',
            None,
        ),
        (500, b'{"error":{"type":"invalid_request_error"}}', None),
        (400, b"not-json", None),
        (400, b"x" * (64 * 1024 + 1), None),
    ],
)
def test_deepseek_context_overflow_signal_requires_exact_bounded_error(
    status: int,
    body: bytes,
    expected: ProviderFailureSignal | None,
) -> None:
    assert DeepSeekProvider._failure_signal(status, body) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_deepseek_executor_maps_context_overflow_to_safe_signal(stream: bool) -> None:
    body = (
        b'{"error":{"message":"This model\'s maximum context length is 1048576 tokens. '
        b"However, you requested 1048793 tokens (792793 in the messages, 256000 in the "
        b'completion). Please reduce the length of the messages or completion.",'
        b'"type":"invalid_request_error","code":"invalid_request_error"}}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=body, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = _provider()
    provider._http_client_pool = ProviderHttpClientPool(lambda: client)
    binding = _binding(provider, WireProtocol.OPENAI_CHAT)
    attempt = await binding.prepare_attempt(
        model=ModelRef("deepseek", "deepseek-flash"),
        payload={
            "model": "deepseek-flash",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": stream,
        },
        stream=stream,
    )

    try:
        assert binding.executor is not None
        with pytest.raises(ProviderError) as raised:
            if stream:
                async for _frame in binding.executor.execute_stream(attempt):
                    pass
            else:
                await binding.executor.execute(attempt)
    finally:
        await provider.close()

    error = raised.value
    assert error.status_code == 400
    assert error.upstream_status_code == 400
    assert error.kind is ProviderFailureKind.CLIENT_REQUEST
    assert error.retryable is False
    assert error.signal is ProviderFailureSignal.CONTEXT_WINDOW_EXCEEDED
    assert error.safe_message == "Request exceeds the selected model's context window"
    assert "1048793" not in error.safe_message

    response = protocol_error_response(error, "openai_chat")
    response_body = bytes(response.body)
    downstream = json.loads(response_body)
    assert response.status_code == 400
    assert response.headers["X-Router-Maestro-Error-Signal"] == "context_window_exceeded"
    assert downstream["error"] == {
        "message": "Request exceeds the selected model's context window",
        "type": "invalid_request_error",
        "param": None,
        "code": "context_length_exceeded",
    }
    assert b"1048793" not in response_body


@pytest.mark.asyncio
async def test_deepseek_anthropic_count_tokens_uses_native_exact_endpoint() -> None:
    provider = _provider()
    request = AnthropicCountTokensRequest(
        model="deepseek-flash",
        messages=[AnthropicUserMessage(content="hello")],
    )

    with (
        patch.object(anthropic_route, "_resolve_provider_name", return_value="deepseek"),
        patch.object(
            anthropic_route,
            "get_router",
            return_value=SimpleNamespace(providers={"deepseek": provider}),
        ),
        patch(
            "router_maestro.providers.deepseek.count_tokens_via_anthropic_api",
            return_value=7,
        ) as exact_count,
    ):
        response = await anthropic_route.count_tokens(request)

    assert response == {"input_tokens": 7}
    exact_count.assert_awaited_once_with(
        base_url="https://deepseek.example/anthropic/v1",
        api_key="upstream-secret",
        model="deepseek-flash",
        messages=[{"role": "user", "content": "hello"}],
        system=None,
        tools=None,
    )
