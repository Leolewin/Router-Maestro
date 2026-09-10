"""Default protocol chains do not imply permission to replay failed requests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from router_maestro.protocols import WireProtocol
from router_maestro.providers.anthropic import AnthropicProvider
from router_maestro.providers.base import BaseProvider, ModelInfo
from router_maestro.providers.bindings import EndpointBinding
from router_maestro.providers.copilot import CopilotProvider
from router_maestro.providers.deepseek import DeepSeekProvider
from router_maestro.providers.handler import ProviderHandler
from router_maestro.routing.capabilities import Operation, ProviderCapabilities
from router_maestro.routing.generation_plan import GenerationCandidate
from router_maestro.routing.model_ref import ModelRef
from router_maestro.routing.transport_policy import DEFAULT_TRANSPORT_POLICY, TransportPolicy


@pytest.mark.parametrize(
    ("ingress", "protocols"),
    [
        (WireProtocol.OPENAI_RESPONSES, (WireProtocol.OPENAI_RESPONSES, WireProtocol.OPENAI_CHAT)),
        (
            WireProtocol.ANTHROPIC_MESSAGES,
            (
                WireProtocol.ANTHROPIC_MESSAGES,
                WireProtocol.OPENAI_RESPONSES,
                WireProtocol.OPENAI_CHAT,
            ),
        ),
        (WireProtocol.OPENAI_CHAT, (WireProtocol.OPENAI_CHAT,)),
        (WireProtocol.GEMINI, (WireProtocol.GEMINI, WireProtocol.OPENAI_CHAT)),
    ],
)
def test_default_protocol_chains_are_provider_neutral(ingress, protocols):
    assert DEFAULT_TRANSPORT_POLICY.protocols(ingress) == protocols
    assert not DEFAULT_TRANSPORT_POLICY.recover_retryable_errors


def _plans(provider, ingress, denied=()):
    info = ModelInfo(
        id="model",
        name="model",
        provider=provider.name,
        transport_capabilities={protocol.value: False for protocol in denied},
    )
    candidate = GenerationCandidate(ModelRef(provider.name, "model"), provider, info)
    return ProviderHandler(provider).bindings_for(candidate, ingress)


def test_deepseek_capability_fallback_is_separate_from_native_error_recovery():
    provider = DeepSeekProvider()
    plans = _plans(provider, WireProtocol.ANTHROPIC_MESSAGES)
    assert [plan.target_protocol for plan in plans] == [
        WireProtocol.ANTHROPIC_MESSAGES,
        WireProtocol.OPENAI_RESPONSES,
        WireProtocol.OPENAI_CHAT,
    ]
    plans = _plans(
        provider, WireProtocol.ANTHROPIC_MESSAGES, denied=(WireProtocol.ANTHROPIC_MESSAGES,)
    )
    assert [plan.target_protocol for plan in plans] == [
        WireProtocol.OPENAI_RESPONSES,
        WireProtocol.OPENAI_CHAT,
    ]
    assert not provider.transport_policy.recover_retryable_errors
    assert not provider.transport_policy.recover_request_rejections


def test_explicit_compatibility_retains_non_chat_models_for_chat_clients():
    plans = _plans(AnthropicProvider(), WireProtocol.OPENAI_CHAT)
    assert [plan.target_protocol for plan in plans] == [WireProtocol.ANTHROPIC_MESSAGES]
    provider = CopilotProvider()
    plans = _plans(provider, WireProtocol.GEMINI)
    assert [plan.target_protocol for plan in plans] == [
        WireProtocol.OPENAI_CHAT,
        WireProtocol.OPENAI_RESPONSES,
        WireProtocol.ANTHROPIC_MESSAGES,
    ]
    assert provider.transport_policy.recover_retryable_errors


def test_pure_plugin_capabilities_are_derived_from_its_declared_bindings():
    class NativeOnly(BaseProvider):
        async def list_models(self):
            return []

        def is_authenticated(self):
            return True

        def bindings(self):
            return (
                EndpointBinding(
                    "native",
                    WireProtocol.OPENAI_RESPONSES,
                    ProviderCapabilities(operations=frozenset({Operation.RESPONSES})),
                ),
            )

    provider = NativeOnly()
    assert provider.capabilities.operations == frozenset({Operation.RESPONSES})
    assert Operation.CHAT not in provider.capabilities.operations


def test_compatibility_adds_only_missing_protocols_and_does_not_enable_recovery():
    policy = replace(DEFAULT_TRANSPORT_POLICY, compatibility_transports=True)
    assert policy == TransportPolicy(compatibility_transports=True)
    for ingress in WireProtocol:
        protocols = policy.protocols(ingress)
        assert len(protocols) == len(set(protocols))
        assert not policy.recover_retryable_errors
