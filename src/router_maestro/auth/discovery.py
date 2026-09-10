"""Shared definitions for provider authentication discovery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from router_maestro.auth.storage import AuthType
from router_maestro.config.providers import ProvidersConfig, default_custom_api_key_env

if TYPE_CHECKING:
    from router_maestro.providers.registry import ProviderRegistry


class ProviderAuthSource(StrEnum):
    """Origin of a provider authentication definition."""

    BUILTIN = "builtin"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class ProviderAuthDefinition:
    """Non-secret information needed to present one provider login flow."""

    provider: str
    display_name: str
    auth_type: AuthType
    credential_required: bool
    source: ProviderAuthSource
    api_key_env: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ProviderAuthDefinition:
        """Validate a server discovery record at the CLI boundary."""
        required = value.get("credential_required")
        if not isinstance(required, bool):
            raise TypeError("credential_required must be a boolean")
        api_key_env = value.get("api_key_env")
        if api_key_env is not None and not isinstance(api_key_env, str):
            raise TypeError("api_key_env must be a string or null")
        return cls(
            provider=str(value["provider"]),
            display_name=str(value["display_name"]),
            auth_type=AuthType(value["auth_type"]),
            credential_required=required,
            source=ProviderAuthSource(value["source"]),
            api_key_env=api_key_env,
        )


def provider_auth_definitions(
    config: ProvidersConfig,
    registry: ProviderRegistry | None = None,
) -> tuple[ProviderAuthDefinition, ...]:
    """Return stable builtin-first definitions for one server configuration."""
    from router_maestro.providers.registry import default_provider_registry

    registry = registry or default_provider_registry()
    custom = []
    for provider_name in sorted(config.providers, key=str.casefold):
        provider = config.providers[provider_name]
        custom.append(
            ProviderAuthDefinition(
                provider=provider_name,
                display_name=provider_name,
                auth_type=AuthType.API_KEY,
                credential_required=not provider.options.allow_unauthenticated,
                source=ProviderAuthSource.CUSTOM,
                api_key_env=(
                    provider.options.api_key_env or default_custom_api_key_env(provider_name)
                ),
            )
        )
    return (*registry.auth_definitions, *custom)


def __getattr__(name: str) -> Any:
    if name == "BUILTIN_PROVIDER_AUTH_DEFINITIONS":
        from router_maestro.providers.registry import default_provider_registry

        return default_provider_registry().auth_definitions
    raise AttributeError(name)
