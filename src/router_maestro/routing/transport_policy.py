"""Shared protocol preferences, separate from upstream failure recovery."""

from dataclasses import dataclass

from router_maestro.protocols import WireProtocol

_DEFAULT_PROTOCOLS = {
    WireProtocol.OPENAI_RESPONSES: (WireProtocol.OPENAI_RESPONSES, WireProtocol.OPENAI_CHAT),
    WireProtocol.ANTHROPIC_MESSAGES: (
        WireProtocol.ANTHROPIC_MESSAGES,
        WireProtocol.OPENAI_RESPONSES,
        WireProtocol.OPENAI_CHAT,
    ),
    WireProtocol.OPENAI_CHAT: (WireProtocol.OPENAI_CHAT,),
    WireProtocol.GEMINI: (WireProtocol.GEMINI, WireProtocol.OPENAI_CHAT),
}
_COMPATIBILITY_PROTOCOLS = (
    WireProtocol.OPENAI_RESPONSES,
    WireProtocol.OPENAI_CHAT,
    WireProtocol.ANTHROPIC_MESSAGES,
)


@dataclass(frozen=True, slots=True)
class TransportPolicy:
    """Opt-ins beyond capability-based protocol fallback.

    Compatibility adds existing cross-protocol paths after the default chain.
    Recovery flags control a *failed attempt*, not transport availability.
    Neither flag permits replay after stream commitment or continuation affinity.
    """

    compatibility_transports: bool = False
    recover_retryable_errors: bool = False
    recover_request_rejections: bool = True

    def protocols(self, ingress: WireProtocol) -> tuple[WireProtocol, ...]:
        preferred = _DEFAULT_PROTOCOLS[ingress]
        if not self.compatibility_transports:
            return preferred
        return (*preferred, *(item for item in _COMPATIBILITY_PROTOCOLS if item not in preferred))


DEFAULT_TRANSPORT_POLICY = TransportPolicy()
COMPATIBILITY_TRANSPORT_POLICY = TransportPolicy(compatibility_transports=True)
LEGACY_TRANSPORT_POLICY = TransportPolicy(
    compatibility_transports=True, recover_retryable_errors=True
)
