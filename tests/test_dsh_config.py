"""Tests for DeepSeek Harness client configuration generation."""

from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from router_maestro.cli import config as cli_config
from router_maestro.cli.client_configs import base as cc_base
from router_maestro.cli.client_configs import get_client, list_clients
from router_maestro.cli.client_configs.base import GenerateContext, IdStyle, ModelSelection
from router_maestro.cli.client_configs.dsh import DshConfig, _dsh_context_window


def _astra() -> dict:
    return {
        "provider": "github-copilot",
        "id": "github-copilot/gpt-6-astra",
        "name": "GPT-6 Astra",
        "max_prompt_tokens": 872_000,
        "max_output_tokens": 128_000,
        "max_context_window_tokens": 1_000_000,
        "context_window_options": [
            {"tier": "default", "max_prompt_tokens": 872_000, "is_default": True}
        ],
        "feature_capabilities": {"vision": True},
        "reasoning_effort_values": ["low", "medium", "high", "xhigh", "max"],
    }


def _sol() -> dict:
    return {
        "provider": "github-copilot",
        "id": "github-copilot/gpt-5.6-sol-fast",
        "name": "GPT-5.6 Sol Fast (Internal only)",
        "max_prompt_tokens": 922_000,
        "max_output_tokens": 128_000,
        "max_context_window_tokens": 1_050_000,
        "context_window_options": [
            {"tier": "default", "max_prompt_tokens": 272_000, "is_default": True},
            {"tier": "long_context", "max_prompt_tokens": 922_000, "is_default": False},
        ],
        "feature_capabilities": {"vision": False},
        "reasoning_effort_values": ["none", "minimal", "low", "medium", "high", "max"],
    }


def _deepseek() -> dict:
    return {
        "provider": "deepseek",
        "id": "deepseek/deepseek-flash",
        "name": "DeepSeek-V4.1-Flash",
        "max_prompt_tokens": None,
        "max_output_tokens": 384_000,
        "max_context_window_tokens": 1_000_000,
        "context_window_options": [],
        "feature_capabilities": {"vision": True},
        "reasoning_effort_values": ["none", "low", "medium", "high", "xhigh", "max"],
    }


def _write(
    path: Path,
    *,
    selected: dict,
    catalog: list[dict],
    source_path: Path | None = None,
) -> None:
    client = DshConfig()
    client._available_models = catalog
    selection = ModelSelection(slot="main", model=selected)
    client.write(
        level="user",
        path=path,
        models={"main": client.resolve_model_selection(selection, IdStyle.QUALIFIED)},
        ctx=GenerateContext(
            id_style=IdStyle.QUALIFIED,
            selections=(selection,),
            extras={"source_path": str(source_path or path)},
            endpoint="https://router.example",
            api_key="not-written",
        ),
    )


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as file:
        return YAML(typ="safe").load(file)


def test_dsh_client_is_registered() -> None:
    assert get_client("dsh") is DshConfig
    assert [client.key for client in list_clients()] == [
        "claude-code",
        "codex",
        "gemini",
        "dsh",
    ]


@pytest.mark.parametrize(
    ("model", "expected"),
    [(_astra(), 1_000_000), (_sol(), 1_050_000), (_deepseek(), 1_000_000)],
)
def test_dsh_context_window_uses_combined_upstream_capacity(model: dict, expected: int) -> None:
    assert _dsh_context_window(model) == expected


def test_dsh_write_preserves_unrelated_settings_and_generates_catalog(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(
        "# keep this comment\n"
        "ui-onboarding:\n"
        "  welcomeNoticeVersion: 2026-08-13.1\n"
        "llm-pi-ai:\n"
        "  providers:\n"
        "    user-provider:\n"
        "      api: openai-responses\n"
        "    router-maestro:\n"
        "      baseURL: https://stale.invalid/v1\n"
        "permission:\n"
        "  defaultPreset: danger-full-access\n"
        "agent-default-model:\n"
        "  provider: old\n"
        "  model: old-model\n",
        encoding="utf-8",
    )

    _write(path, selected=_astra(), catalog=[_astra(), _sol()])

    text = path.read_text(encoding="utf-8")
    document = _load(path)
    assert text.startswith("# keep this comment\n")
    assert document["ui-onboarding"]["welcomeNoticeVersion"] == "2026-08-13.1"
    assert document["permission"]["defaultPreset"] == "danger-full-access"
    providers = document["llm-pi-ai"]["providers"]
    assert providers["user-provider"]["api"] == "openai-responses"
    rm = providers["router-maestro"]
    assert rm["api"] == "openai-responses"
    assert rm["baseURL"] == "https://router.example/api/openai/v1"
    assert rm["apiKeyEnv"] == "ROUTER_MAESTRO_API_KEY"
    assert "apiKey" not in rm
    assert "maxTokens" not in text
    by_id = {model["id"]: model for model in rm["models"]}
    assert by_id["github-copilot/gpt-6-astra"]["contextWindow"] == 1_000_000
    assert by_id["github-copilot/gpt-6-astra"]["input"] == ["text", "image"]
    assert by_id["github-copilot/gpt-5.6-sol-fast"]["contextWindow"] == 1_050_000
    assert by_id["github-copilot/gpt-5.6-sol-fast"]["name"] == "GPT-5.6 Sol Fast"
    assert by_id["github-copilot/gpt-5.6-sol-fast"]["input"] == ["text"]
    assert by_id["github-copilot/gpt-5.6-sol-fast"]["reasoningEfforts"] == {
        "off": "none",
        "minimal": "minimal",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "max": "max",
    }
    assert document["agent-default-model"] == {
        "provider": "router-maestro",
        "model": "github-copilot/gpt-6-astra",
    }


def test_dsh_writes_deepseek_total_context_window(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"

    _write(path, selected=_deepseek(), catalog=[_deepseek()])

    models = _load(path)["llm-pi-ai"]["providers"]["router-maestro"]["models"]
    assert models == [
        {
            "id": "deepseek/deepseek-flash",
            "name": "DeepSeek-V4.1-Flash",
            "contextWindow": 1_000_000,
            "input": ["text", "image"],
            "reasoningEfforts": {
                "off": "none",
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": "xhigh",
                "max": "max",
            },
        }
    ]


def test_dsh_preview_can_read_original_target_without_modifying_it(tmp_path: Path) -> None:
    source = tmp_path / "settings.yaml"
    preview = tmp_path / "preview.yaml"
    original = "shell:\n  timeoutMs: 120000\n"
    source.write_text(original, encoding="utf-8")

    _write(preview, selected=_sol(), catalog=[_sol()], source_path=source)

    assert source.read_text(encoding="utf-8") == original
    assert _load(preview)["shell"]["timeoutMs"] == 120_000


def test_dsh_rejects_project_level_write(tmp_path: Path) -> None:
    client = DshConfig()
    selection = ModelSelection(slot="main", model=_astra())
    with pytest.raises(ValueError, match="user-level"):
        client.write(
            level="project",
            path=tmp_path / "settings.yaml",
            models={"main": "github-copilot/gpt-6-astra"},
            ctx=GenerateContext(id_style=IdStyle.QUALIFIED, selections=(selection,)),
        )


def test_dsh_cli_command_writes_user_settings(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(cc_base.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cc_base, "_fetch_and_display_models", lambda: [_astra(), _sol()])
    monkeypatch.setattr(cc_base.Prompt, "ask", lambda *args, **kwargs: "2")
    monkeypatch.setattr(cc_base.Confirm, "ask", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        cc_base,
        "get_admin_client",
        lambda: type("AdminClient", (), {"endpoint": "https://router.example"})(),
    )

    cli_config.dsh_config(id_style=IdStyle.BARE)

    document = _load(home / ".dsh" / "settings.yaml")
    assert document["agent-default-model"] == {
        "provider": "router-maestro",
        "model": "github-copilot/gpt-5.6-sol-fast",
    }
    models = document["llm-pi-ai"]["providers"]["router-maestro"]["models"]
    assert {model["id"] for model in models} == {
        "github-copilot/gpt-6-astra",
        "github-copilot/gpt-5.6-sol-fast",
    }
