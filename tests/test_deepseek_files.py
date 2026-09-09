"""DeepSeek Files API proxy contracts."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI

from router_maestro.auth.storage import ApiKeyCredential
from router_maestro.providers import ProviderError, ProviderFailureKind
from router_maestro.providers.deepseek import DeepSeekProvider
from router_maestro.providers.http_executor import ProviderHttpClientPool
from router_maestro.server.dependencies import get_app_router
from router_maestro.server.middleware import verify_api_key
from router_maestro.server.routes.files import router as files_router


def _provider(upstream_client: httpx.AsyncClient) -> DeepSeekProvider:
    provider = DeepSeekProvider(base_url="https://deepseek.example/")
    provider.auth_manager.get_credential = lambda _name: ApiKeyCredential(  # type: ignore[method-assign]
        key="upstream-secret"
    )
    provider._http_client_pool = ProviderHttpClientPool(lambda: upstream_client)
    return provider


def _app(provider: DeepSeekProvider) -> FastAPI:
    app = FastAPI()
    app.include_router(files_router, dependencies=[Depends(verify_api_key)])
    app.dependency_overrides[get_app_router] = lambda: SimpleNamespace(
        providers={"deepseek": provider}
    )
    return app


@pytest.mark.asyncio
async def test_deepseek_files_routes_proxy_complete_lifecycle_and_rewrite_auth(
    monkeypatch,
) -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": body,
            }
        )
        if request.method == "POST":
            payload = b'{"id":"file-api-one","object":"file","bytes":4}'
        elif request.method == "DELETE":
            payload = b'{"id":"file-api-one","object":"file","deleted":true}'
        elif request.url.path.endswith("/file-api-one"):
            payload = b'{"id":"file-api-one","object":"file","bytes":4}'
        else:
            payload = b'{"object":"list","data":[],"has_more":false}'
        return httpx.Response(
            200,
            content=payload,
            headers={"content-type": "application/json", "x-ratelimit-remaining": "9"},
            request=request,
        )

    monkeypatch.setenv("ROUTER_MAESTRO_API_KEY", "downstream-router-key")
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = _provider(upstream_client)
    transport = httpx.ASGITransport(app=_app(provider), raise_app_exceptions=False)
    multipart = (
        b"--boundary\r\n"
        b'Content-Disposition: form-data; name="purpose"\r\n\r\n'
        b"user_data\r\n"
        b"--boundary\r\n"
        b'Content-Disposition: form-data; name="file"; filename="tiny.png"\r\n'
        b"Content-Type: image/png\r\n\r\n"
        b"data\r\n--boundary--\r\n"
    )
    headers = {
        "Authorization": "Bearer downstream-router-key",
        "User-Agent": "DeepSeekHarness/test",
        "X-DeepSeek-Harness-User-Id": "anonymous-id",
    }

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            upload = await client.post(
                "/api/openai/v1/files",
                content=multipart,
                headers={**headers, "Content-Type": "multipart/form-data; boundary=boundary"},
            )
            listing = await client.get(
                "/api/openai/v1/files?purpose=user_data&limit=1&order=asc",
                headers=headers,
            )
            retrieve = await client.get(
                "/api/openai/v1/files/file-api-one",
                headers=headers,
            )
            delete = await client.delete(
                "/api/openai/v1/files/file-api-one",
                headers=headers,
            )
    finally:
        await provider.close()

    assert [response.status_code for response in (upload, listing, retrieve, delete)] == [
        200,
        200,
        200,
        200,
    ]
    assert upload.content == b'{"id":"file-api-one","object":"file","bytes":4}'
    assert listing.json() == {"object": "list", "data": [], "has_more": False}
    assert delete.json()["deleted"] is True
    assert upload.headers["x-ratelimit-remaining"] == "9"

    assert [(item["method"], item["url"]) for item in requests] == [
        ("POST", "https://deepseek.example/files"),
        ("GET", "https://deepseek.example/files?purpose=user_data&limit=1&order=asc"),
        ("GET", "https://deepseek.example/files/file-api-one"),
        ("DELETE", "https://deepseek.example/files/file-api-one"),
    ]
    assert requests[0]["body"] == multipart
    recorded_headers = [item["headers"] for item in requests if isinstance(item["headers"], dict)]
    assert recorded_headers[0]["content-type"] == "multipart/form-data; boundary=boundary"
    assert all(headers["authorization"] == "Bearer upstream-secret" for headers in recorded_headers)
    assert all(headers["user-agent"] == "DeepSeekHarness/test" for headers in recorded_headers)
    assert all(
        headers["x-deepseek-harness-user-id"] == "anonymous-id" for headers in recorded_headers
    )
    assert "downstream-router-key" not in repr(requests)


@pytest.mark.asyncio
async def test_deepseek_files_route_requires_router_maestro_auth(monkeypatch) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    monkeypatch.setenv("ROUTER_MAESTRO_API_KEY", "downstream-router-key")
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = _provider(upstream_client)
    transport = httpx.ASGITransport(app=_app(provider), raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/openai/v1/files")
    finally:
        await provider.close()

    assert response.status_code == 401
    assert calls == 0


@pytest.mark.asyncio
async def test_deepseek_files_route_preserves_upstream_error_body_and_retry_header(
    monkeypatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            content=b'{"error":{"message":"storage quota exceeded","type":"files_error"}}',
            headers={"content-type": "application/json", "retry-after": "12"},
            request=request,
        )

    monkeypatch.setenv("ROUTER_MAESTRO_API_KEY", "downstream-router-key")
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = _provider(upstream_client)
    transport = httpx.ASGITransport(app=_app(provider), raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/openai/v1/files",
                content=b"multipart-body",
                headers={
                    "Authorization": "Bearer downstream-router-key",
                    "Content-Type": "multipart/form-data; boundary=test",
                },
            )
    finally:
        await provider.close()

    assert response.status_code == 429
    assert response.json() == {
        "error": {"message": "storage quota exceeded", "type": "files_error"}
    }
    assert response.headers["retry-after"] == "12"


@pytest.mark.asyncio
@pytest.mark.parametrize("file_id", ["../models", "file-api-one/../../models", "other-one"])
async def test_deepseek_files_rejects_non_provider_file_ids_before_upstream(
    file_id: str,
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = _provider(upstream_client)
    try:
        with pytest.raises(ProviderError) as raised:
            await provider.files_request(
                method="GET",
                file_id=file_id,
                downstream_headers={},
            )
    finally:
        await provider.close()

    assert raised.value.status_code == 400
    assert raised.value.kind is ProviderFailureKind.CLIENT_REQUEST
    assert raised.value.parameter == "file_id"
    assert calls == 0
