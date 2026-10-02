"""Generated OpenCode configuration.

OpenCode merges config from several places and *later wins*: global config, then
`OPENCODE_CONFIG`, then the project's own `opencode.json`, then the project's
`.opencode/` directory. The agent owns /workspace, so writing the global config
restricts nothing -- and `.opencode/plugin*/*.ts` is auto-discovered and executed,
which no config key closes.

So the lockdown is three things together: a read-only config bound into the
managed config directory, which outranks project config; the undocumented
OPENCODE_DISABLE_PROJECT_CONFIG, which is the only thing that stops project
config and the plugin directory from being read at all; and OPENCODE_PERMISSION,
which is applied last of all. Verified against opencode v1.18.31.
"""

from __future__ import annotations

import json
from pathlib import Path

from .policy import PolicyError, is_private


MANAGED_CONFIG = "/etc/opencode/opencode.json"
DENIED_TOOLS = ("webfetch", "websearch")

# Allowed outright, because the sandbox already bounds them: inside the
# container these tools can only reach /workspace, /input (read-only), /output,
# /jobs and /logs. There is no host filesystem, no $HOME, no credentials and no
# scheduler. Prompting a human per shell command would add friction without
# adding a control, and trains them to approve on reflex.
CONTAINED_TOOLS = (
    "bash", "edit", "write", "read", "glob", "grep", "list", "task", "todowrite", "skill",
)

# Denied whatever else is configured: this is the one that would leave the
# sandbox, so it is not the agent's to ask for.
ESCAPE_TOOLS = ("external_directory",)


def agent_policy(site: dict[str, object]) -> dict[str, object]:
    value = site.get("agent", {})
    if not isinstance(value, dict):
        raise PolicyError("site 'agent' must be a mapping")
    return value


def denied_tools(site: dict[str, object]) -> list[str]:
    configured = agent_policy(site).get("deny_tools", list(DENIED_TOOLS))
    if not isinstance(configured, list):
        raise PolicyError("site 'agent.deny_tools' must be a list")
    return [str(tool) for tool in configured]


def permissions(site: dict[str, object]) -> dict[str, str]:
    """The complete permission block, not a partial one.

    OpenCode deep-merges permissions, so naming only the denials would leave
    everything else at its default of asking.
    """
    allowed = {tool: "allow" for tool in CONTAINED_TOOLS}
    denied = {tool: "deny" for tool in list(denied_tools(site)) + list(ESCAPE_TOOLS)}
    override = agent_policy(site).get("permission", {})
    if not isinstance(override, dict):
        raise PolicyError("site 'agent.permission' must be a mapping")
    return {**allowed, **denied, **{str(k): str(v) for k, v in override.items()}}


def opencode_config(site: dict[str, object]) -> dict[str, object]:
    agent = agent_policy(site)
    config: dict[str, object] = {
        "$schema": "https://opencode.ai/config.json",
        "permission": permissions(site),
        "mcp": _mcp(agent),
    }

    provider = agent.get("provider")
    if isinstance(provider, dict) and provider.get("id"):
        name = str(provider["id"])
        if not provider.get("base_url"):
            raise PolicyError(f"provider {name!r} needs a base_url")
        # enabled_providers is a real allowlist: "ONLY these providers will be enabled".
        config["enabled_providers"] = [name]
        config["provider"] = {
            name: {
                "npm": str(provider.get("npm", "@ai-sdk/openai-compatible")),
                "options": {"baseURL": str(provider["base_url"])},
                "models": {str(model): {} for model in provider.get("models", [])},
            }
        }
    return config


def write_config(sandbox_path: Path, site: dict[str, object]) -> Path | None:
    if not is_private(site):
        return None
    path = sandbox_path / "agent" / "opencode.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(opencode_config(site), indent=2) + "\n", encoding="utf-8")
    return path


def container_env(site: dict[str, object]) -> dict[str, str]:
    """Environment that outranks anything the agent can write into /workspace."""
    if not is_private(site):
        return {}
    return {
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        # Applied last of all, so it carries the whole block.
        "OPENCODE_PERMISSION": json.dumps(permissions(site)),
    }


def config_mount(sandbox_path: Path, site: dict[str, object]) -> list[str]:
    if not is_private(site):
        return []
    return ["--bind", f"{sandbox_path}/agent/opencode.json:{MANAGED_CONFIG}:ro"]


def _mcp(agent: dict[str, object]) -> dict[str, object]:
    """Declared MCP servers.

    There is no global MCP off-switch and no allowlist in OpenCode: servers are
    declared *by config*, so the only real control is stopping untrusted config
    from loading at all. This block is the complete set under that lockdown.
    """
    servers = agent.get("mcp", {})
    if not isinstance(servers, dict):
        raise PolicyError("site 'agent.mcp' must be a mapping")
    return servers
