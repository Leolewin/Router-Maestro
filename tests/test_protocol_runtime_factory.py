"""Request-scoped protocol runtime and capsule provenance contracts."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, cast

import pytest

from router_maestro.protocols import FrozenJsonValue, OpaqueState, WireProtocol
from router_maestro.protocols.openai_responses import OpenAIResponsesRuntime
from router_maestro.providers.bindings import (
    COPILOT_OPENAI_RESPONSES_BINDING,
    EndpointBinding,
    ProtocolRuntimeOptions,
)
from router_maestro.routing.capabilities import Operation, ProviderCapabilities
from router_maestro.routing.router import Router
from router_maestro.runtime.reasoning_capsule import ReasoningCapsuleCodec
from router_maestro.server.protocols.runtime_factory import ProtocolRuntimeFactory


def _factory() -> ProtocolRuntimeFactory:
    return ProtocolRuntimeFactory(
        ReasoningCapsuleCodec(bytes([23]) * 32),
        {
            (
                "github-copilot",
                COPILOT_OPENAI_RESPONSES_BINDING,
            ): WireProtocol.OPENAI_RESPONSES
        },
        {
            ("github-copilot", COPILOT_OPENAI_RESPONSES_BINDING): ProtocolRuntimeOptions(
                allow_per_event_response_ids=True,
                defer_intermediate_item_ids=True,
            )
        },
    )


def _state(blob: FrozenJsonValue | bytes | None = None) -> OpaqueState:
    opaque_blob = blob
    if opaque_blob is None:
        opaque_blob = cast(
            FrozenJsonValue,
            {
                "type": "reasoning",
                "id": "rs_123",
                "summary": [{"type": "summary_text", "text": "summary"}],
                "encrypted_content": "opaque-state",
                "future_field": {"preserved": True},
            },
        )
    return OpaqueState(
        origin_protocol=WireProtocol.OPENAI_RESPONSES,
        origin_provider="github-copilot",
        origin_model="gpt-test",
        item_id="rs_123",
        blob=opaque_blob,
        origin_binding=COPILOT_OPENAI_RESPONSES_BINDING,
    )


def test_capsule_hook_round_trips_full_raw_reasoning_item() -> None:
    factory = _factory()
    state = _state()

    capsule = factory.encode_opaque_state(
        state,
        protocol=WireProtocol.ANTHROPIC_MESSAGES,
        model="gpt-test",
        item_id="rs_123",
    )
    restored = factory.decode_opaque_state(
        capsule,
        protocol=WireProtocol.ANTHROPIC_MESSAGES,
        model="github-copilot/gpt-test",
        item_id="synthetic-anthropic-block-id",
    )

    assert capsule.startswith("rmr1.")
    assert restored.origin_protocol is WireProtocol.OPENAI_RESPONSES
    assert restored.origin_provider == "github-copilot"
    assert restored.origin_binding == COPILOT_OPENAI_RESPONSES_BINDING
    assert restored.origin_model == "gpt-test"
    assert restored.item_id == "rs_123"
    assert isinstance(restored.blob, Mapping)
    future_field = restored.blob["future_field"]
    assert isinstance(future_field, Mapping)
    assert future_field["preserved"] is True


def test_capsule_hook_round_trips_binary_state() -> None:
    factory = _factory()
    state = _state(blob=b"\x00\xffopaque")

    capsule = factory.encode_opaque_state(
        state,
        protocol=WireProtocol.GEMINI,
        model="gpt-test",
        item_id="rs_123",
    )
    restored = factory.decode_opaque_state(
        capsule,
        protocol=WireProtocol.GEMINI,
        model="ignored-public-model",
        item_id="ignored-carrier-id",
    )

    assert restored.blob == b"\x00\xffopaque"


@pytest.mark.parametrize(
    ("model", "item_id"),
    [("other-model", "rs_123"), ("gpt-test", "rs_other")],
)
def test_capsule_encoder_fails_closed_on_provenance_mismatch(model: str, item_id: str) -> None:
    with pytest.raises(ValueError, match="provenance"):
        _factory().encode_opaque_state(
            _state(),
            protocol=WireProtocol.ANTHROPIC_MESSAGES,
            model=model,
            item_id=item_id,
        )


def test_capsule_decoder_rejects_unknown_provider_binding() -> None:
    codec = ReasoningCapsuleCodec(bytes([23]) * 32)
    known = _factory()
    capsule = known.encode_opaque_state(
        _state(),
        protocol=WireProtocol.ANTHROPIC_MESSAGES,
        model="gpt-test",
        item_id="rs_123",
    )
    unknown = ProtocolRuntimeFactory(codec, {})

    with pytest.raises(ValueError, match="^Invalid reasoning capsule$"):
        unknown.decode_opaque_state(
            capsule,
            protocol=WireProtocol.ANTHROPIC_MESSAGES,
            model="gpt-test",
            item_id="rs_123",
        )


def test_responses_target_runtime_is_frozen_to_provider_and_binding() -> None:
    factory = _factory()
    runtime = factory._build(  # narrow construction contract; TransportPlan is tested elsewhere
        WireProtocol.OPENAI_RESPONSES,
        model=None,
        stream=False,
        provider="github-copilot",
        binding=COPILOT_OPENAI_RESPONSES_BINDING,
    )

    assert isinstance(runtime, OpenAIResponsesRuntime)
    assert runtime.provider_name == "github-copilot"
    assert runtime.binding_id == COPILOT_OPENAI_RESPONSES_BINDING
    assert runtime.allow_per_event_response_ids is True
    assert runtime.defer_intermediate_item_ids is True


def test_responses_id_quirk_is_not_enabled_for_other_provider_bindings() -> None:
    runtime = _factory()._build(
        WireProtocol.OPENAI_RESPONSES,
        model=None,
        stream=False,
        provider="openai",
        binding="openai-responses",
    )

    assert isinstance(runtime, OpenAIResponsesRuntime)
    assert runtime.allow_per_event_response_ids is False
    assert runtime.defer_intermediate_item_ids is False


@pytest.mark.parametrize(
    "second_protocol", [WireProtocol.OPENAI_CHAT, WireProtocol.OPENAI_RESPONSES]
)
def test_duplicate_binding_provenance_is_rejected_before_runtime_construction(second_protocol):
    first = EndpointBinding(
        "duplicate",
        WireProtocol.OPENAI_RESPONSES,
        ProviderCapabilities(operations=frozenset({Operation.RESPONSES})),
    )
    second = EndpointBinding("duplicate", second_protocol, first.capabilities)
    provider = SimpleNamespace(bindings=lambda: (first, second))
    router = SimpleNamespace(providers={"example": provider})
    with pytest.raises(ValueError, match="duplicate binding"):
        ProtocolRuntimeFactory.for_router(
            cast(Router, router), ReasoningCapsuleCodec(bytes([22]) * 32)
        )


@pytest.mark.parametrize(
    "field",
    ["allow_reasoning_opaque", "allow_per_event_response_ids", "defer_intermediate_item_ids"],
)
def test_runtime_options_do_not_accept_truthy_non_boolean_values(field):
    with pytest.raises(TypeError, match="booleans"):
        ProtocolRuntimeOptions(**cast(dict[str, Any], {field: "true"}))


@pytest.mark.parametrize(
    ("protocol", "options"),
    [
        (WireProtocol.OPENAI_RESPONSES, ProtocolRuntimeOptions(allow_reasoning_opaque=True)),
        (WireProtocol.OPENAI_CHAT, ProtocolRuntimeOptions(allow_per_event_response_ids=True)),
        (WireProtocol.ANTHROPIC_MESSAGES, ProtocolRuntimeOptions(defer_intermediate_item_ids=True)),
    ],
)
def test_codec_deviations_cannot_be_attached_to_an_unrelated_protocol(protocol, options):
    with pytest.raises(ValueError, match="binding"):
        EndpointBinding(
            "invalid-options",
            protocol,
            ProviderCapabilities(operations=frozenset()),
            runtime_options=options,
        )
