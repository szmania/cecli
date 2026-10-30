"""Implicit ``wf:{name}`` workflow sub-agents.

Each *enabled* workflow becomes a ``wf:{name}`` worker sub-agent, mirroring
the ``ws:`` workspace registration (:mod:`cecli.helpers.workspaces.subagents`).
The declarative ``skills`` / ``servers`` / ``root`` fields map onto the
sub-agent's ``metadata["agent-config"]``; ``skip_cli_confirmations`` is always
forced because workflow workers run unattended.

The most recent configs are cached module-level (``_workflow_configs_by_agent``
 including disabled workflows) so ``/workflows`` and session restore can look
the definitions back up without re-reading YAML.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# agent name ("wf:{name}") -> WorkflowConfig, populated by register. Cached for
# *all* configs (enabled or not) so list/enable/restore can find them again.
_workflow_configs_by_agent: Dict[str, Any] = {}


def register_workflow_subagents(workflow_configs) -> List[str]:
    """Create or replace a ``wf:{name}`` sub-agent for each enabled workflow.

    Idempotent: re-registering simply replaces the registry entry (running
    workers keep their own coders). Disabled workflows are not registered but
    are still cached. Returns the list of registered agent names.
    """
    from cecli.helpers.agents.config import SubAgentConfig
    from cecli.helpers.agents.service import AgentService

    # Make sure the built-in defaults (including ``worker``) are loaded so the
    # workflow agents can mirror them, regardless of call ordering.
    if "worker" not in AgentService.get_registry():
        AgentService.build_registry([])

    worker = AgentService.get_registry().get("worker")

    registered: List[str] = []
    for wf in workflow_configs or []:
        agent_name = f"wf:{wf.name}"
        _workflow_configs_by_agent[agent_name] = wf

        if not wf.enabled:
            continue

        metadata = dict(worker.metadata) if worker else {}
        agent_config = dict(metadata.get("agent-config") or {})

        # declarative mapping
        if wf.skills:
            agent_config["skills_init"] = list(wf.skills)
            agent_config.setdefault("skills_includelist", [])
        if wf.servers:
            agent_config["servers_includelist"] = [s.lower() for s in wf.servers]
        # workflow workers run unattended: never block on an interactive confirm
        agent_config["skip_cli_confirmations"] = True
        # ...and are detached: their results never surface in the parent's
        # conversation (background operations stay out of its context).
        agent_config["detached"] = True
        if wf.root:
            metadata["root"] = str(Path(wf.root).expanduser().resolve())

        metadata["agent-config"] = agent_config
        metadata["description"] = f"Workflow sub-agent for '{wf.name}'"
        # ``allow_nested_delegation`` is intentionally NOT forced here: the
        # service-side wf: merge defaults it via setdefault so an explicit
        # opt-out in the metadata survives.

        AgentService.register_subagent(
            agent_name,
            SubAgentConfig(
                name=agent_name,
                prompt=worker.prompt if worker else "",
                model=wf.model or (worker.model if worker else None),
                hooks=dict(wf.hooks) if wf.hooks else (dict(worker.hooks) if worker else {}),
                auto_reap=False,  # persist for the run; the runner reaps at the end
                metadata=metadata,
            ),
        )
        registered.append(agent_name)
        logger.info("Registered workflow sub-agent '%s'", agent_name)

    return registered


def unregister_workflow_subagents(workflow_configs) -> List[str]:
    """Remove ``wf:{name}`` entries from the agent registry.

    Accepts a list of WorkflowConfig (or plain agent names). The configs stay
    in the module cache so ``/workflows enable`` / session restore can bring
    them back. Returns the names that were actually removed.
    """
    from cecli.helpers.agents.service import AgentService

    registry = AgentService.get_registry()
    removed: List[str] = []
    for item in workflow_configs or []:
        agent_name = item if isinstance(item, str) else f"wf:{item.name}"
        if registry.pop(agent_name, None) is not None:
            removed.append(agent_name)

    return removed


def ensure_wf_agent_registered(name: str) -> Any:
    """Re-register a cached ``wf:`` agent if the registry lost it.

    Used on session restore (analogue of ``ensure_ws_agent_registered``):
    resolves *name* (``wf:{workflow}`` or a bare workflow name) against the
    module cache populated by :func:`register_workflow_subagents`.
    """
    from cecli.helpers.agents.service import AgentService

    agent_name = name if name.startswith("wf:") else f"wf:{name}"
    if agent_name in AgentService.get_registry():
        return agent_name

    wf = _workflow_configs_by_agent.get(agent_name)
    if wf is None:
        return None

    register_workflow_subagents([wf])
    return agent_name if agent_name in AgentService.get_registry() else None


def get_workflow_configs() -> List[Any]:
    """Return the cached WorkflowConfigs from the most recent registrations."""
    return list(_workflow_configs_by_agent.values())
