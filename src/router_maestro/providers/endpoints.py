"""Declarations for authenticated provider-owned HTTP extensions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from router_maestro.protocols import WireProtocol

if TYPE_CHECKING:
    from fastapi import Request, Response

    from router_maestro.providers.base import BaseProvider


class EndpointHandler(Protocol):
    async def __call__(self, provider: BaseProvider, request: Request) -> Response: ...


@dataclass(frozen=True, slots=True)
class ProviderEndpoint:
    """One route owned by a plugin; authentication cannot be opted out of."""

    name: str
    path: str
    methods: frozenset[str]
    handler: EndpointHandler
    error_protocol: WireProtocol = WireProtocol.OPENAI_CHAT

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.name):
            raise ValueError("endpoint name must be a non-empty identifier")
        if (
            not self.path.startswith("/api/")
            or self.path.startswith("/api/admin/")
            or self.path == "/api/admin"
            or any(part in {".", "..", ""} for part in self.path.split("/")[1:])
            or "?" in self.path
            or "#" in self.path
        ):
            raise ValueError("extension endpoints require a non-admin /api/ path")
        parameters = re.findall(r"\{([^{}]*)\}", self.path)
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) for value in parameters):
            raise ValueError("extension endpoints support only single-segment path parameters")
        remaining = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "", self.path)
        if "{" in remaining or "}" in remaining or len(parameters) != len(set(parameters)):
            raise ValueError("invalid extension endpoint path parameters")
        if any(
            "{" in segment and not re.fullmatch(r"\{[A-Za-z_][A-Za-z0-9_]*\}", segment)
            for segment in self.path.split("/")
        ):
            raise ValueError("extension path parameters must occupy a complete segment")
        methods = frozenset(self.methods)
        if not methods or not methods <= {
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "HEAD",
            "OPTIONS",
        }:
            raise ValueError("extension endpoint methods must be explicit HTTP methods")
        if not callable(self.handler):
            raise TypeError("extension endpoint requires a handler")
        if not isinstance(self.error_protocol, WireProtocol):
            raise TypeError("extension endpoint requires a wire error protocol")
        object.__setattr__(self, "methods", methods)
