"""Bundled plugins use the same registrations as application-supplied plugins."""

from router_maestro.auth.discovery import ProviderAuthDefinition, ProviderAuthSource
from router_maestro.auth.storage import AuthType
from router_maestro.providers.anthropic import AnthropicProvider
from router_maestro.providers.copilot import CopilotProvider
from router_maestro.providers.custom_factory import create_custom_provider
from router_maestro.providers.deepseek import DeepSeekProvider
from router_maestro.providers.deepseek_endpoints import DEEPSEEK_ENDPOINTS
from router_maestro.providers.openai import OpenAIProvider
from router_maestro.providers.registry import ProviderPlugin, ProviderRegistry


def builtin_provider_registry() -> ProviderRegistry:
    return ProviderRegistry(
        (
            ProviderPlugin(
                id=provider.name,
                factory=provider,
                auth=ProviderAuthDefinition(
                    provider=provider.name,
                    display_name=display_name,
                    auth_type=auth_type,
                    credential_required=True,
                    source=ProviderAuthSource.BUILTIN,
                ),
                endpoints=endpoints,
            )
            for provider, display_name, auth_type, endpoints in (
                (CopilotProvider, "GitHub Copilot", AuthType.OAUTH, ()),
                (OpenAIProvider, "OpenAI", AuthType.API_KEY, ()),
                (AnthropicProvider, "Anthropic", AuthType.API_KEY, ()),
                (DeepSeekProvider, "DeepSeek", AuthType.API_KEY, DEEPSEEK_ENDPOINTS),
            )
        ),
        configured_factories={"openai-compatible": create_custom_provider},
    )
