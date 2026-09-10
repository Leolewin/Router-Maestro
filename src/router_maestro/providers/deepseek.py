"""DeepSeek provider with native Chat, Responses, and Messages transports."""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, Mapping
from copy import deepcopy
from typing import Any, NoReturn, cast
from urllib.parse import quote

import httpx

from router_maestro.auth import ApiKeyCredential, AuthManager, AuthType
from router_maestro.protocols import ConversionMode, WireProtocol
from router_maestro.providers.base import (
    BaseProvider,
    ModelInfo,
    ProviderError,
    ProviderFailureKind,
)
from router_maestro.providers.bindings import (
    AttemptRequestContext,
    EndpointBinding,
    PreparedAttempt,
)
from router_maestro.providers.http_executor import SharedHttpExecutor
from router_maestro.providers.openai_base import OpenAIChatProvider, _request_audit
from router_maestro.routing.capabilities import Feature, Operation, ProviderCapabilities
from router_maestro.routing.model_ref import ModelRef
from router_maestro.routing.transport_policy import TransportPolicy
from router_maestro.utils import get_logger
from router_maestro.utils.reasoning import budget_to_effort
from router_maestro.utils.token_config import count_tokens_via_anthropic_api

logger = get_logger("providers.deepseek")

DEEPSEEK_API_URL = "https://api.deepseek.com"
DEEPSEEK_ANTHROPIC_MESSAGES_BINDING = "deepseek-anthropic-messages"
DEEPSEEK_OPENAI_CHAT_BINDING = "deepseek-openai-chat"
DEEPSEEK_OPENAI_RESPONSES_BINDING = "deepseek-openai-responses"
DEEPSEEK_FILES_TIMEOUT = httpx.Timeout(connect=30.0, read=240.0, write=600.0, pool=30.0)
_DEEPSEEK_FILE_ID_PATTERN = re.compile(r"file-api-[A-Za-z0-9._~-]+\Z")

_DEEPSEEK_BINDING_SPECS = {
    DEEPSEEK_ANTHROPIC_MESSAGES_BINDING: (
        WireProtocol.ANTHROPIC_MESSAGES,
        "/anthropic/v1/messages",
    ),
    DEEPSEEK_OPENAI_CHAT_BINDING: (WireProtocol.OPENAI_CHAT, "/chat/completions"),
    DEEPSEEK_OPENAI_RESPONSES_BINDING: (WireProtocol.OPENAI_RESPONSES, "/responses"),
}
_DEEPSEEK_MODEL_IDS = (
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-v4-flash-vision-exp",
)
_DEEPSEEK_DISPLAY_NAMES = {
    "deepseek-v4-flash": "DeepSeek-V4-Flash",
    "deepseek-v4-pro": "DeepSeek-V4-Pro",
    "deepseek-v4-flash-vision-exp": "DeepSeek-V4-Flash-Vision-Exp",
}
_DEEPSEEK_CONTEXT_TOKENS = 1_000_000
_DEEPSEEK_MAX_OUTPUT_TOKENS = 384_000
_DEEPSEEK_REASONING_EFFORTS = ["none", "low", "medium", "high", "xhigh", "max"]
_DEEPSEEK_DSH_HEADERS = (
    "user-agent",
    "x-deepseek-harness-user-id",
    "x-deepseek-harness-session-id",
    "x-deepseek-harness-compact",
)


def _model_info(model_id: str) -> ModelInfo:
    """Attach documented V4 metadata when the live catalog returns a known model."""
    known = model_id in _DEEPSEEK_DISPLAY_NAMES
    vision = model_id == "deepseek-v4-flash-vision-exp"
    if not known:
        return ModelInfo(id=model_id, name=model_id, provider="deepseek")

    operations = {
        operation.value: True
        for operation in (
            Operation.CHAT,
            Operation.CHAT_STREAM,
            Operation.RESPONSES,
            Operation.RESPONSES_STREAM,
            Operation.NATIVE_ANTHROPIC,
        )
    }
    features = {
        Feature.TOOLS.value: True,
        Feature.VISION.value: vision,
        Feature.REASONING.value: True,
        Feature.FILES.value: vision,
    }
    transports = {
        WireProtocol.ANTHROPIC_MESSAGES.value: True,
        WireProtocol.OPENAI_CHAT.value: True,
        WireProtocol.OPENAI_RESPONSES.value: True,
    }
    return ModelInfo(
        id=model_id,
        name=_DEEPSEEK_DISPLAY_NAMES[model_id],
        provider="deepseek",
        max_output_tokens=_DEEPSEEK_MAX_OUTPUT_TOKENS,
        max_context_window_tokens=_DEEPSEEK_CONTEXT_TOKENS,
        supports_thinking=True,
        supports_vision=vision,
        reasoning_effort_values=list(_DEEPSEEK_REASONING_EFFORTS),
        supported_endpoints=("/anthropic/v1/messages", "/chat/completions", "/responses"),
        operation_capabilities=operations,
        feature_capabilities=features,
        transport_capabilities=transports,
    )


class DeepSeekProvider(OpenAIChatProvider):
    """DeepSeek's official API as one multi-protocol built-in provider."""

    name = "deepseek"

    @property
    def transport_policy(self) -> TransportPolicy:
        """Allow capability fallback, but never replay a failed native attempt."""
        return TransportPolicy(recover_request_rejections=False)

    def __init__(self, base_url: str = DEEPSEEK_API_URL) -> None:
        super().__init__(base_url=base_url, logger=logger)
        self.auth_manager = AuthManager()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(operations=frozenset(Operation))

    def bindings(self) -> tuple[EndpointBinding, ...]:
        """Expose all generation formats implemented by the official API."""
        bindings = getattr(self, "_generation_bindings", None)
        if bindings is not None:
            return bindings

        dialect = DeepSeekProviderDialect(self)
        executor = DeepSeekHttpExecutor(self)
        bindings = (
            EndpointBinding(
                id=DEEPSEEK_ANTHROPIC_MESSAGES_BINDING,
                protocol=WireProtocol.ANTHROPIC_MESSAGES,
                capabilities=ProviderCapabilities(
                    operations=frozenset({Operation.NATIVE_ANTHROPIC})
                ),
                dialect=dialect,
                executor=executor,
            ),
            EndpointBinding(
                id=DEEPSEEK_OPENAI_CHAT_BINDING,
                protocol=WireProtocol.OPENAI_CHAT,
                capabilities=ProviderCapabilities(
                    operations=frozenset({Operation.CHAT, Operation.CHAT_STREAM})
                ),
                dialect=dialect,
                executor=executor,
            ),
            EndpointBinding(
                id=DEEPSEEK_OPENAI_RESPONSES_BINDING,
                protocol=WireProtocol.OPENAI_RESPONSES,
                capabilities=ProviderCapabilities(
                    operations=frozenset({Operation.RESPONSES, Operation.RESPONSES_STREAM})
                ),
                dialect=dialect,
                executor=executor,
            ),
        )
        self._generation_bindings = bindings
        return bindings

    def model_aliases(self) -> Mapping[str, str]:
        """Pin DSH's official bare model IDs to this provider."""
        return {model_id: model_id for model_id in _DEEPSEEK_MODEL_IDS}

    def is_authenticated(self) -> bool:
        credential = self.auth_manager.get_credential(self.name)
        return credential is not None and credential.type == AuthType.API_KEY

    def _get_api_key(self) -> str:
        credential = self.auth_manager.get_credential(self.name)
        if not credential or credential.type != AuthType.API_KEY:
            logger.error("Not authenticated with DeepSeek")
            raise ProviderError(
                "Not authenticated with DeepSeek",
                status_code=401,
                kind=ProviderFailureKind.AUTHENTICATION,
                provider=self.name,
            )
        return cast(ApiKeyCredential, credential).key

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_api_key()}",
            "Content-Type": "application/json",
        }

    def _anthropic_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._get_api_key(),
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    async def files_request(
        self,
        *,
        method: str,
        file_id: str | None = None,
        query: str = "",
        downstream_headers: Mapping[str, str],
        content: AsyncIterable[bytes] | None = None,
    ) -> httpx.Response:
        """Proxy one OpenAI-compatible Files API operation without buffering uploads."""
        if method not in {"GET", "POST", "DELETE"}:
            raise ValueError(f"Unsupported DeepSeek Files API method {method!r}")
        if method == "POST" and file_id is not None:
            raise ValueError("DeepSeek Files API upload cannot target a file ID")
        if file_id is not None and _DEEPSEEK_FILE_ID_PATTERN.fullmatch(file_id) is None:
            raise ProviderError(
                "Invalid DeepSeek file ID",
                status_code=400,
                kind=ProviderFailureKind.CLIENT_REQUEST,
                provider=self.name,
                parameter="file_id",
            )

        path = "/files"
        if file_id is not None:
            path = f"{path}/{quote(file_id, safe='')}"
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        normalized_headers = {name.lower(): value for name, value in downstream_headers.items()}
        headers = {
            "Authorization": f"Bearer {self._get_api_key()}",
            "Accept": normalized_headers.get("accept", "application/json"),
        }
        for name in ("content-type", "content-length", *_DEEPSEEK_DSH_HEADERS):
            value = normalized_headers.get(name)
            if value:
                headers[name] = value

        request_options: dict[str, Any] = {
            "headers": headers,
            "timeout": DEEPSEEK_FILES_TIMEOUT,
        }
        if content is not None:
            request_options["content"] = content

        try:
            async with self._http_client_pool.lease() as client:
                return await client.request(method, url, **request_options)
        except httpx.TimeoutException as error:
            self._raise_timeout_error(
                "DeepSeek Files API",
                error,
                logger,
                provider=self.name,
            )
        except httpx.HTTPError as error:
            self._raise_http_error(
                "DeepSeek Files API",
                error,
                logger,
                provider=self.name,
            )

    @property
    def anthropic_base_url(self) -> str:
        """Base URL consumed by the shared native Messages token-count client."""
        return f"{self.base_url}/anthropic/v1"

    async def count_tokens(
        self, protocol: WireProtocol, payload: Mapping[str, Any], *, model: str
    ) -> int | None:
        if protocol is not WireProtocol.ANTHROPIC_MESSAGES:
            return None
        return await count_tokens_via_anthropic_api(
            base_url=self.anthropic_base_url,
            api_key=self._get_api_key(),
            model=model,
            messages=payload.get("messages", []),
            system=payload.get("system"),
            tools=payload.get("tools"),
        )

    def _error_label(self) -> str:
        return "DeepSeek"

    async def list_models(self) -> list[ModelInfo]:
        """Fetch the authoritative model IDs and enrich documented V4 entries."""
        url = f"{self.base_url}/models"
        headers = self._get_headers()
        audit = _request_audit()
        if audit is not None:
            audit.record_upstream("GET", url, headers, None)
        async with self._http_client_pool.lease() as client:
            try:
                response = await client.get(url, headers=headers, timeout=30.0)
                if audit is not None:
                    audit.record_upstream_response(
                        response.status_code,
                        dict(response.headers),
                        response.content,
                    )
                response.raise_for_status()
                models = [_model_info(model_id) for model_id in self._parse_model_catalog(response)]
                logger.info("Fetched %d DeepSeek models", len(models))
                return models
            except httpx.HTTPError as error:
                status_code = (
                    error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
                )
                logger.warning(
                    "Failed to list DeepSeek models, using documented defaults (%s, status=%s)",
                    type(error).__name__,
                    status_code,
                )
                return [_model_info(model_id) for model_id in _DEEPSEEK_MODEL_IDS]


class DeepSeekProviderDialect:
    """Copy-on-write preparation for DeepSeek's three native wire formats."""

    id = "deepseek"

    def __init__(self, provider: DeepSeekProvider) -> None:
        self.provider = provider

    async def prepare_attempt(
        self,
        *,
        binding_id: str,
        protocol: WireProtocol,
        model: ModelRef,
        payload: Mapping[str, Any],
        stream: bool,
        request_context: AttemptRequestContext,
    ) -> PreparedAttempt:
        expected_protocol, path = self._binding_spec(binding_id)
        if protocol is not expected_protocol:
            raise ValueError("DeepSeek binding protocol does not match its binding ID")
        if model.provider != self.provider.name:
            raise ValueError("DeepSeek attempt model belongs to another provider")

        body = deepcopy(dict(payload))
        body["model"] = model.upstream_id
        body["stream"] = stream
        if (
            protocol is WireProtocol.OPENAI_CHAT
            and request_context.conversion_mode is ConversionMode.SEMANTIC_IR
        ):
            self._normalize_converted_chat_reasoning(body)

        headers = (
            self.provider._anthropic_headers()
            if protocol is WireProtocol.ANTHROPIC_MESSAGES
            else self.provider._get_headers()
        )
        self._copy_protocol_headers(headers, request_context, protocol)
        return PreparedAttempt(
            binding_id=binding_id,
            protocol=protocol,
            model=model,
            url=f"{self.provider.base_url}{path}",
            payload=body,
            headers=headers,
            stream=stream,
            _payload_owned=True,
        )

    @staticmethod
    def _binding_spec(binding_id: str) -> tuple[WireProtocol, str]:
        try:
            return _DEEPSEEK_BINDING_SPECS[binding_id]
        except KeyError:
            raise ValueError(f"Unknown DeepSeek binding {binding_id!r}") from None

    @staticmethod
    def _copy_protocol_headers(
        headers: dict[str, str],
        request_context: AttemptRequestContext,
        protocol: WireProtocol,
    ) -> None:
        for name in _DEEPSEEK_DSH_HEADERS:
            value = request_context.header(name)
            if value:
                headers[name] = value
        if protocol is WireProtocol.ANTHROPIC_MESSAGES:
            for name in ("anthropic-version", "anthropic-beta"):
                value = request_context.header(name)
                if value:
                    headers[name] = value

    @staticmethod
    def _normalize_converted_chat_reasoning(body: dict[str, Any]) -> None:
        """Map protocol-neutral budgets onto DeepSeek's Chat reasoning controls."""
        thinking = body.get("thinking")
        if not isinstance(thinking, Mapping):
            return
        thinking = dict(thinking)
        budget = thinking.pop("budget_tokens", None)
        body["thinking"] = thinking
        if body.get("reasoning_effort") is None and isinstance(budget, int) and budget > 0:
            body["reasoning_effort"] = budget_to_effort(budget) or "low"
        if body.get("reasoning_effort") == "minimal":
            body["reasoning_effort"] = "low"


class DeepSeekHttpExecutor(SharedHttpExecutor):
    """Raw JSON/SSE executor shared by all DeepSeek generation bindings."""

    def __init__(self, provider: DeepSeekProvider) -> None:
        self.provider = provider
        super().__init__(client_pool=provider._http_client_pool)

    def _skip_raw_sse_data(self, data: str, attempt: PreparedAttempt) -> bool:
        del attempt
        return data == "[DONE]"

    def _skip_sse_frame(
        self,
        frame: Mapping[str, Any],
        attempt: PreparedAttempt,
    ) -> bool:
        return attempt.protocol is WireProtocol.ANTHROPIC_MESSAGES and frame.get("type") == "ping"

    def _validate_attempt(self, attempt: PreparedAttempt, *, stream: bool) -> None:
        expected_protocol, _path = DeepSeekProviderDialect._binding_spec(attempt.binding_id)
        if attempt.protocol is not expected_protocol:
            raise ValueError("DeepSeek attempt protocol does not match its binding ID")
        if attempt.model.provider != self.provider.name:
            raise ValueError("DeepSeek executor received another provider's model")
        if attempt.method != "POST":
            raise ValueError("DeepSeek generation bindings require POST")
        if attempt.stream is not stream:
            raise ValueError("DeepSeek attempt stream mode does not match execution")

    def _raise_status(
        self,
        error: httpx.HTTPStatusError,
        attempt: PreparedAttempt,
        *,
        stream: bool,
    ) -> NoReturn:
        self.provider._raise_http_status_error(
            "DeepSeek",
            error,
            logger,
            stream=stream,
            include_body=True,
            provider=self.provider.name,
            model=attempt.model.upstream_id,
        )

    def _raise_timeout(
        self,
        error: httpx.TimeoutException,
        attempt: PreparedAttempt,
        *,
        stream: bool,
    ) -> NoReturn:
        self.provider._raise_timeout_error(
            "DeepSeek",
            error,
            logger,
            stream=stream,
            provider=self.provider.name,
            model=attempt.model.upstream_id,
        )

    def _raise_http_error(
        self,
        error: httpx.HTTPError,
        attempt: PreparedAttempt,
        *,
        stream: bool,
    ) -> NoReturn:
        self.provider._raise_http_error(
            "DeepSeek",
            error,
            logger,
            stream=stream,
            provider=self.provider.name,
            model=attempt.model.upstream_id,
        )

    def _raise_protocol_error(self, error: Exception, attempt: PreparedAttempt) -> NoReturn:
        BaseProvider._raise_protocol_error(
            self.provider.name,
            attempt.model.upstream_id,
            error,
        )


__all__ = [
    "DEEPSEEK_API_URL",
    "DEEPSEEK_ANTHROPIC_MESSAGES_BINDING",
    "DEEPSEEK_OPENAI_CHAT_BINDING",
    "DEEPSEEK_OPENAI_RESPONSES_BINDING",
    "DeepSeekHttpExecutor",
    "DeepSeekProvider",
    "DeepSeekProviderDialect",
]
