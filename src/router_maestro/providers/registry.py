"""Explicit, application-scoped provider plugins. No import scanning or hot loading."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from router_maestro.providers.endpoints import ProviderEndpoint
from router_maestro.routing.model_ref import validate_provider_id

if TYPE_CHECKING:
    from router_maestro.auth.discovery import ProviderAuthDefinition
    from router_maestro.auth.repository import CredentialRepository
    from router_maestro.config.providers import CustomProviderConfig
    from router_maestro.providers.base import BaseProvider


class ConfiguredProviderFactory(Protocol):
    def __call__(
        self,
        provider_name: str,
        provider_config: CustomProviderConfig,
        *,
        credential_repository: CredentialRepository,
    ) -> BaseProvider | None: ...


@dataclass(frozen=True, slots=True)
class ProviderPlugin:
    """A named provider factory, login metadata, and optional HTTP extensions.

    The returned provider owns its catalog, bindings, credentials, and close().
    Factories must not open resources until they are used by a Router generation.
    """

    id: str
    factory: Callable[[], BaseProvider]
    auth: ProviderAuthDefinition
    endpoints: tuple[ProviderEndpoint, ...] = ()

    def __post_init__(self) -> None:
        validate_provider_id(self.id)
        if self.auth.provider != self.id:
            raise ValueError("plugin authentication metadata must use its provider ID")
        if not callable(self.factory):
            raise TypeError("provider plugin requires a factory")
        object.__setattr__(self, "endpoints", tuple(self.endpoints))
        names = [endpoint.name for endpoint in self.endpoints]
        if len(names) != len(set(names)):
            raise ValueError("provider plugin declares duplicate endpoint names")


class ProviderRegistry:
    """Immutable registration snapshot shared by routes and Router generations."""

    def __init__(
        self,
        plugins: Iterable[ProviderPlugin] = (),
        *,
        configured_factories: Mapping[str, ConfiguredProviderFactory] | None = None,
    ) -> None:
        self._plugins = tuple(plugins)
        names = [plugin.id.casefold() for plugin in self._plugins]
        if len(names) != len(set(names)):
            raise ValueError("provider plugin IDs must be unique case-insensitively")
        if any(
            not name or not callable(factory)
            for name, factory in (configured_factories or {}).items()
        ):
            raise ValueError("configured provider factories need non-empty types and callables")
        self._configured_factories = MappingProxyType(dict(configured_factories or {}))

    @property
    def plugins(self) -> tuple[ProviderPlugin, ...]:
        return self._plugins

    @property
    def provider_ids(self) -> frozenset[str]:
        return frozenset(plugin.id.casefold() for plugin in self.plugins)

    @property
    def auth_definitions(self) -> tuple[ProviderAuthDefinition, ...]:
        return tuple(plugin.auth for plugin in self.plugins)

    def with_plugins(self, *plugins: ProviderPlugin) -> ProviderRegistry:
        return ProviderRegistry(
            (*self.plugins, *plugins), configured_factories=self._configured_factories
        )

    def with_configured_factory(
        self, provider_type: str, factory: ConfiguredProviderFactory
    ) -> ProviderRegistry:
        if not provider_type or provider_type in self._configured_factories:
            raise ValueError("configured provider type must be non-empty and unique")
        return ProviderRegistry(
            self.plugins,
            configured_factories={**self._configured_factories, provider_type: factory},
        )

    def supports_configured_type(self, provider_type: str) -> bool:
        return provider_type in self._configured_factories

    def create_configured(
        self,
        provider_name: str,
        config: CustomProviderConfig,
        *,
        credential_repository: CredentialRepository,
    ) -> BaseProvider | None:
        if provider_name.casefold() in self.provider_ids:
            raise ValueError(f"configured provider {provider_name!r} conflicts with a plugin")
        factory = self._configured_factories.get(config.type)
        if factory is None:
            return None
        provider = factory(provider_name, config, credential_repository=credential_repository)
        if provider is not None and provider.name != provider_name:
            raise ValueError("configured provider factory returned a different provider ID")
        return provider


def default_provider_registry() -> ProviderRegistry:
    """Build the bundled registration declarations without creating providers."""
    from router_maestro.providers.builtin import builtin_provider_registry

    return builtin_provider_registry()
