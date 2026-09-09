"""DeepSeek OpenAI-compatible Files API passthrough."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from router_maestro.providers import DeepSeekProvider, ProviderError, ProviderFailureKind
from router_maestro.routing.router import Router
from router_maestro.server.dependencies import get_app_router

router = APIRouter()

_RESPONSE_HEADERS = frozenset({"content-type", "retry-after"})


def _deepseek_provider(model_router: Router) -> DeepSeekProvider:
    provider = model_router.providers.get("deepseek")
    if not isinstance(provider, DeepSeekProvider):
        raise ProviderError(
            "DeepSeek provider is unavailable",
            status_code=503,
            retryable=True,
            kind=ProviderFailureKind.UNKNOWN,
            provider="deepseek",
        )
    return provider


async def _proxy_files_request(
    request: Request,
    *,
    file_id: str | None,
    model_router: Router,
) -> Response:
    provider = _deepseek_provider(model_router)
    upstream = await provider.files_request(
        method=request.method,
        file_id=file_id,
        query=request.url.query,
        downstream_headers=request.headers,
        content=request.stream() if request.method == "POST" else None,
    )
    response_headers = {
        name: value
        for name, value in upstream.headers.items()
        if name.lower() in _RESPONSE_HEADERS or name.lower().startswith("x-ratelimit-")
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )


@router.post("/api/openai/v1/files")
async def upload_file(
    request: Request,
    model_router: Router = Depends(get_app_router),
) -> Response:
    """Upload an image into the DeepSeek API-key file namespace."""
    return await _proxy_files_request(request, file_id=None, model_router=model_router)


@router.get("/api/openai/v1/files")
async def list_files(
    request: Request,
    model_router: Router = Depends(get_app_router),
) -> Response:
    """List files stored for the configured DeepSeek API key."""
    return await _proxy_files_request(request, file_id=None, model_router=model_router)


@router.get("/api/openai/v1/files/{file_id}")
async def retrieve_file(
    file_id: str,
    request: Request,
    model_router: Router = Depends(get_app_router),
) -> Response:
    """Retrieve metadata for one DeepSeek file."""
    return await _proxy_files_request(request, file_id=file_id, model_router=model_router)


@router.delete("/api/openai/v1/files/{file_id}")
async def delete_file(
    file_id: str,
    request: Request,
    model_router: Router = Depends(get_app_router),
) -> Response:
    """Delete one file from the configured DeepSeek API-key namespace."""
    return await _proxy_files_request(request, file_id=file_id, model_router=model_router)
