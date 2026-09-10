"""DeepSeek-owned Files API extension and compatibility aliases."""

from fastapi import Request, Response

from router_maestro.providers.base import BaseProvider, ProviderError, ProviderFailureKind
from router_maestro.providers.deepseek import DeepSeekProvider
from router_maestro.providers.endpoints import ProviderEndpoint

_RESPONSE_HEADERS = frozenset({"content-type", "retry-after"})


async def proxy_files(provider: BaseProvider, request: Request) -> Response:
    if not isinstance(provider, DeepSeekProvider):
        raise ProviderError(
            "DeepSeek Files provider is unavailable",
            status_code=503,
            kind=ProviderFailureKind.UNKNOWN,
            provider=provider.name,
        )
    upstream = await provider.files_request(
        method=request.method,
        file_id=request.path_params.get("file_id"),
        query=request.url.query,
        downstream_headers=request.headers,
        content=request.stream() if request.method == "POST" else None,
    )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={
            name: value
            for name, value in upstream.headers.items()
            if name.lower() in _RESPONSE_HEADERS or name.lower().startswith("x-ratelimit-")
        },
    )


DEEPSEEK_ENDPOINTS = tuple(
    ProviderEndpoint(
        name=f"{namespace}-{operation}",
        path=f"{prefix}{suffix}",
        methods=methods,
        handler=proxy_files,
    )
    for namespace, prefix in (
        ("files", "/api/providers/deepseek/v1/files"),
        ("files-compat", "/api/openai/v1/files"),
    )
    for operation, suffix, methods in (
        ("collection", "", frozenset({"POST", "GET"})),
        ("item", "/{file_id}", frozenset({"GET", "DELETE"})),
    )
)
