"""Mount plugin declarations through one authenticated, generation-safe boundary."""

from collections.abc import Awaitable, Callable, Iterator, Sequence
from typing import Protocol, cast

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from starlette.routing import BaseRoute, Route

from router_maestro.protocols import WireProtocol
from router_maestro.providers.base import ProviderError, ProviderFailureKind
from router_maestro.providers.endpoints import ProviderEndpoint
from router_maestro.providers.registry import ProviderRegistry
from router_maestro.routing.router import Router
from router_maestro.server.dependencies import get_app_router
from router_maestro.server.middleware import verify_api_key
from router_maestro.server.protocols.errors import ProtocolSurface


class _RouteContext(Protocol):
    path: str
    methods: set[str] | None


def _overlapping_paths(left: str, right: str) -> bool:
    """Reject identical and parameter-shadowed paths, not just identical strings."""
    left_parts = left.strip("/").split("/")
    right_parts = right.strip("/").split("/")
    for first, second in zip(left_parts, right_parts):
        if first.endswith(":path}") or second.endswith(":path}"):
            return True
        if first != second and "{" not in first and "{" not in second:
            return False
    return len(left_parts) == len(right_parts)


def _handler(provider_id: str, endpoint: ProviderEndpoint) -> Callable[..., Awaitable[Response]]:
    async def handle(request: Request, model_router: Router = Depends(get_app_router)) -> Response:
        provider = model_router.providers.get(provider_id)
        if provider is None:
            raise ProviderError(
                "Endpoint provider is unavailable",
                status_code=503,
                provider=provider_id,
                kind=ProviderFailureKind.UNKNOWN,
            )
        return await endpoint.handler(provider, request)

    handle.__name__ = f"{provider_id}_{endpoint.name}"
    return handle


def _route_signatures(routes: Sequence[BaseRoute]) -> Iterator[tuple[str, set[str]]]:
    for route in routes:
        if isinstance(route, Route):
            yield route.path, set(route.methods or ())
            continue
        # Newer FastAPI versions keep included routers lazy. Their effective
        # contexts include all nested include_router prefixes, unlike raw routes.
        effective_contexts = getattr(route, "effective_route_contexts", None)
        if callable(effective_contexts):
            contexts = cast(Callable[[], Iterator[_RouteContext]], effective_contexts)
            for context in contexts():
                yield context.path, set(context.methods or ())


def build_provider_endpoints(
    registry: ProviderRegistry, *, existing_routes: Sequence[BaseRoute] = ()
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(verify_api_key)])
    occupied = list(_route_signatures(existing_routes))
    for plugin in registry.plugins:
        for endpoint in plugin.endpoints:
            for path, methods in occupied:
                if methods.intersection(endpoint.methods) and _overlapping_paths(
                    path, endpoint.path
                ):
                    raise ValueError(
                        f"Provider endpoint {plugin.id}/{endpoint.name} conflicts with {path}"
                    )
            occupied.append((endpoint.path, set(endpoint.methods)))
            for method in sorted(endpoint.methods):
                router.add_api_route(
                    endpoint.path,
                    _handler(plugin.id, endpoint),
                    methods=[method],
                    name=f"provider:{plugin.id}:{endpoint.name}:{method}",
                    operation_id=f"provider_{plugin.id}_{endpoint.name}_{method.lower()}",
                    response_model=None,
                )
    return router


def mount_provider_endpoints(app: FastAPI, registry: ProviderRegistry) -> None:
    app.include_router(build_provider_endpoints(registry, existing_routes=app.routes))


def extension_error_surface(request: Request) -> ProtocolSurface | None:
    registry = getattr(request.app.state, "provider_registry", None)
    if not isinstance(registry, ProviderRegistry):
        return None
    path_match: ProviderEndpoint | None = None
    for plugin in registry.plugins:
        for endpoint in plugin.endpoints:
            if _overlapping_paths(endpoint.path, request.url.path):
                if path_match is None:
                    path_match = endpoint
                if request.method in endpoint.methods:
                    return _error_surface(endpoint)
    return _error_surface(path_match) if path_match is not None else None


def _error_surface(endpoint: ProviderEndpoint) -> ProtocolSurface:
    if endpoint.error_protocol is WireProtocol.ANTHROPIC_MESSAGES:
        return "anthropic"
    return cast(ProtocolSurface, endpoint.error_protocol.value)
