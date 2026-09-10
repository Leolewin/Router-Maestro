"""Auth module for router-maestro."""

from router_maestro.auth.discovery import (
    ProviderAuthDefinition,
    ProviderAuthSource,
    provider_auth_definitions,
)
from router_maestro.auth.manager import AuthManager, run_async
from router_maestro.auth.repository import CredentialRepository
from router_maestro.auth.storage import (
    ApiKeyCredential,
    AuthStorage,
    AuthType,
    Credential,
    OAuthCredential,
)

__all__ = [
    "AuthManager",
    "CredentialRepository",
    "AuthStorage",
    "AuthType",
    "Credential",
    "OAuthCredential",
    "ApiKeyCredential",
    "run_async",
    "ProviderAuthDefinition",
    "ProviderAuthSource",
    "provider_auth_definitions",
]


def __getattr__(name: str):
    if name == "BUILTIN_PROVIDER_AUTH_DEFINITIONS":
        from router_maestro.providers.registry import default_provider_registry

        return default_provider_registry().auth_definitions
    raise AttributeError(name)
