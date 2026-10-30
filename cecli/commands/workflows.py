"""/workflows — inspect and run declarative workflows.

Subcommands: list, run <name>, show <name>, status [name], last <name>,
enable/disable <name>, reload. ``run`` starts a background task so the REPL
is never blocked by a workflow.
"""

import asyncio
import logging

from cecli.commands.utils.base_command import BaseCommand

logger = logging.getLogger(__name__)

# Strong refs to in-flight background ``/workflows run`` tasks (the event loop
# only weakly references tasks, so an unreferenced one can be GC'd mid-run).
_bg_runs: set = set()


class WorkflowCommand(BaseCommand):
    NORM_NAME = "workflows"
    DESCRIPTION = "List, run and manage declarative workflows"
    show_completion_notification = True

    SUBCOMMANDS = ("list", "run", "show", "status", "last", "enable", "disable", "reload")
    NAME_SUBCOMMANDS = ("run", "show", "status", "last", "enable", "disable")

    @classmethod
    async def execute(cls, io, coder, args, **kwargs):
        """Dispatch a workflows subcommand.

        Syntax:
            /workflows list             — Show loaded workflows (default)
            /workflows run <name>       — Start a manual run in the background
            /workflows show <name>      — Dump the workflow definition (YAML)
            /workflows status [name]    — Scheduler job state (config view if absent)
            /workflows last <name>      — Summary of the most recent run
            /workflows enable <name>    — Enable a workflow (this session)
            /workflows disable <name>   — Disable a workflow (this session)
            /workflows reload           — Re-read workflow definitions
        """
        parts = (args or "").strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else "list"
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "list":
            await cls._cmd_list(io, coder)
        elif sub == "run":
            await cls._cmd_run(io, coder, rest)
        elif sub == "show":
            cls._cmd_show(io, coder, rest)
        elif sub == "status":
            await cls._cmd_status(io, coder, rest)
        elif sub == "last":
            await cls._cmd_last(io, coder, rest)
        elif sub in ("enable", "disable"):
            await cls._cmd_toggle(io, coder, rest, enable=(sub == "enable"))
        elif sub == "reload":
            cls._cmd_reload(io, coder)
        else:
            io.tool_error(f"Unknown subcommand '{sub}'. Try one of: {', '.join(cls.SUBCOMMANDS)}")

    @classmethod
    def get_help(cls) -> str:
        """Get help text for the workflows command."""
        help_text = super().get_help()
        help_text += "\nUsage:\n"
        help_text += "  /workflows list              # Show loaded workflows\n"
        help_text += "  /workflows run <name>        # Start a manual run in the background\n"
        help_text += "  /workflows show <name>       # Dump the workflow definition (YAML)\n"
        help_text += "  /workflows status [name]     # Scheduler job state or config view\n"
        help_text += "  /workflows last <name>       # Summary of the most recent run\n"
        help_text += "  /workflows enable <name>     # Enable a workflow (this session)\n"
        help_text += "  /workflows disable <name>    # Disable a workflow (this session)\n"
        help_text += "  /workflows reload            # Re-read workflow definitions\n"
        return help_text

    @classmethod
    def get_completions(cls, io, coder, args) -> list:
        """Complete subcommands, then workflow names for the name-taking ones."""
        partial = (args or "").strip()

        if " " not in partial:
            return [s for s in cls.SUBCOMMANDS if s.startswith(partial)] or list(cls.SUBCOMMANDS)

        sub, _, rest = partial.partition(" ")
        if sub.lower() in cls.NAME_SUBCOMMANDS:
            names = sorted(wf.name for wf in cls._workflows(coder))
            return [n for n in names if n.startswith(rest)]

        return []

    # ------------------------------------------------------------------ #
    # Subcommands
    # ------------------------------------------------------------------ #

    @classmethod
    async def _cmd_list(cls, io, coder) -> None:
        """Print each loaded workflow with its schedule and (if known) last run."""
        workflows = cls._workflows(coder)
        if not workflows:
            io.tool_output("No workflows are loaded.")
            io.tool_output(
                "Add YAML files to ~/.cecli/workflows/ or a 'workflows:' config"
                " section, then run /workflows reload."
            )
            return

        jobs = await cls._job_rows(coder)

        io.print("Workflows:")
        for wf in sorted(workflows, key=lambda w: w.name):
            job = jobs.get(wf.name) or {}
            state = "enabled" if wf.enabled else "disabled"
            line = f"  - {wf.name} [{state}] schedule: {wf.schedule or 'manual'}"
            if job:
                line += (
                    f" | last: {job.get('last_run') or '-'}" f" ({job.get('last_status') or '-'})"
                )
            io.print(line)
            if wf.description:
                io.print(f"    {wf.description.strip()}")

    @classmethod
    async def _cmd_run(cls, io, coder, name) -> None:
        """Start a background manual run of the named workflow."""
        from cecli.helpers.workflows.runner import execute_workflow

        wf = cls._require(io, coder, name, "run")
        if wf is None:
            return

        task = asyncio.create_task(execute_workflow(coder, wf, source="manual"))

        # The loop keeps only weak references to tasks; hold one here so a
        # background run can never be garbage-collected mid-flight, and report
        # the summary into the conversation when it finishes.
        _bg_runs.add(task)
        task.add_done_callback(lambda t: cls._report_run(io, t, wf.name))
        io.tool_output(f"workflow '{wf.name}' started in the background")

    @classmethod
    def _report_run(cls, io, task, name) -> None:
        """Done-callback for background manual runs: release ref + announce."""
        _bg_runs.discard(task)

        try:
            result = task.result()
        except asyncio.CancelledError:
            io.tool_warning(f"workflow '{name}' run was cancelled")

            return
        except Exception as exc:
            io.tool_error(f"workflow '{name}' run failed: {exc}")

            return

        line = f"workflow '{name}' finished: {result.status}"
        summary = (result.summary or "").strip().replace("\n", " ")

        if summary:
            line += f" \u2014 {summary[:240]}"

        if result.status == "ok":
            io.tool_output(line)
        else:
            io.tool_error(line)

    @classmethod
    def _cmd_show(cls, io, coder, name) -> None:
        """Dump the workflow definition as YAML."""
        import yaml

        wf = cls._require(io, coder, name, "show")
        if wf is None:
            return

        io.tool_output(yaml.dump(wf.to_dict(), sort_keys=False, default_flow_style=False))

    @classmethod
    async def _cmd_status(cls, io, coder, name) -> None:
        """Show scheduler job rows and active leases, or fall back to the list view."""
        if name and not cls._find(coder, name):
            io.tool_error(cls._unknown_message(coder, name))
            return

        jobs = await cls._job_rows(coder, name=name or None)
        leases = await cls._active_leases(coder, name=name or None)

        if not jobs and not leases:
            # No scheduler DB state visible — degrade to the config view.
            if name:
                cls._cmd_show(io, coder, name)
            else:
                await cls._cmd_list(io, coder)
                io.tool_output("(scheduler not running — showing config view)")
            return

        if jobs:
            io.print("Scheduler jobs:")
            for job_name, row in sorted(jobs.items()):
                io.print(
                    f"  - {job_name}: enabled={row.get('enabled')}"
                    f" engine={row.get('engine') or '-'}"
                    f" next_run={row.get('next_run') or '-'}"
                    f" last_run={row.get('last_run') or '-'}"
                    f" ({row.get('last_status') or '-'})"
                )

        if leases:
            io.print("Active leases:")
            for lease in leases:
                io.print(
                    f"  - {lease.get('name')}: slot={lease.get('slot')}"
                    f" instance={lease.get('instance_id')}"
                    f" expires={lease.get('lease_expires_at')}"
                )

    @classmethod
    async def _cmd_last(cls, io, coder, name) -> None:
        """Summarize the most recent run of a workflow (DB row, else run log)."""
        from cecli.helpers.workflows import logs

        if not name:
            io.tool_error("Usage: /workflows last <name>")
            return

        wf = cls._find(coder, name)
        lookup = wf.name if wf else name.removeprefix("wf:")

        row = await cls._recent_run(coder, lookup)
        if row is not None:
            io.print(f"Last run of '{lookup}' ({row.get('finished_at') or row.get('started_at')}):")
            io.print(f"  Status:  {row.get('status')}")
            io.print(f"  Source:  {row.get('source')}")
            if row.get("summary"):
                io.print(f"  Summary: {row.get('summary')}")
            return

        path = logs.latest_log_path(lookup)
        if path is None:
            io.tool_output(f"No recorded runs for '{lookup}'.")
            return

        lines = logs.read_log_lines(path)
        finish = next((ln for ln in reversed(lines) if ln.get("event") == "finish"), {})
        io.print(f"Last run of '{lookup}' (log: {path}):")
        io.print(f"  Status:  {finish.get('status', '?')}")
        io.print(f"  Source:  {finish.get('source', '?')}")
        if finish.get("summary"):
            io.print(f"  Summary: {finish.get('summary')}")
        for ln in lines:
            if ln.get("event") == "task":
                io.print(f"  task[{ln.get('idx')}] {ln.get('label')}: {ln.get('status')}")

    @classmethod
    async def _cmd_toggle(cls, io, coder, name, enable) -> None:
        """Flip wf.enabled for the session and (un)register the wf: agent."""
        from cecli.helpers.workflows.subagents import (
            register_workflow_subagents,
            unregister_workflow_subagents,
        )

        wf = cls._require(io, coder, name, "enable" if enable else "disable")
        if wf is None:
            return

        wf.enabled = bool(enable)
        if enable:
            register_workflow_subagents([wf])
        else:
            unregister_workflow_subagents([wf])

        db = cls._scheduler_db(coder)
        if db is not None:
            method = getattr(db, "enable" if enable else "disable", None)
            if method is not None:
                try:
                    await (
                        method(wf.name) if enable else method(wf.name, "disabled via /workflows")
                    )
                except TypeError:
                    try:
                        await method(wf.name)
                    except Exception:
                        logger.warning("scheduler db sync failed", exc_info=True)
                except Exception:
                    logger.warning("scheduler db sync failed", exc_info=True)

        state = "enabled" if enable else "disabled"
        io.tool_output(
            f"Workflow '{wf.name}' {state} for this session (files unchanged —"
            " /workflows reload re-reads them)."
        )

    @classmethod
    def _cmd_reload(cls, io, coder) -> None:
        """Re-read workflow definitions and refresh the wf: agent registrations."""
        try:
            from cecli.helpers.workflows import load_workflows

            workflows = load_workflows(
                config_arg=getattr(coder, "workflows_config_arg", None),
                merged_config=getattr(coder, "workflows_merged_config", None),
                git_root=getattr(coder, "root", None),
            )
        except Exception as exc:
            io.tool_error(f"Error reloading workflows: {exc}")
            return

        from cecli.helpers.workflows.subagents import (
            get_workflow_configs,
            register_workflow_subagents,
            unregister_workflow_subagents,
        )

        # Files re-read here may not cover sources the startup load had access
        # to (inline --workflows / merged config); keep those cached configs so
        # they don't silently vanish from the registry on reload.
        previous = get_workflow_configs()
        by_name = {wf.name: wf for wf in previous}
        by_name.update({wf.name: wf for wf in workflows})
        merged = sorted(by_name.values(), key=lambda w: w.name)

        unregister_workflow_subagents(previous)
        registered = register_workflow_subagents(merged)

        # A running scheduler must learn the new definitions too (hash change
        # reseeds next_run; vanished names get disabled) - fire-and-forget.
        scheduler = getattr(coder, "scheduler", None)
        if scheduler is not None and getattr(scheduler, "db", None) is not None:
            import asyncio

            try:
                asyncio.get_running_loop().create_task(
                    scheduler.db.sync_definitions({wf.name: wf for wf in merged if wf.enabled})
                )
            except RuntimeError:
                pass

        try:
            coder.workflows = merged
        except Exception:
            pass

        io.tool_output(
            f"Reloaded {len(merged)} workflow(s);"
            f" {len(registered)} agent(s) registered: {', '.join(registered) or 'none'}"
        )

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    @classmethod
    def _workflows(cls, coder) -> list:
        """The loaded WorkflowConfigs: coder-provided first, else the register cache."""
        workflows = getattr(coder, "workflows", None)
        if isinstance(workflows, (list, tuple)):
            return list(workflows)

        from cecli.helpers.workflows.subagents import get_workflow_configs

        return get_workflow_configs()

    @classmethod
    def _find(cls, coder, name):
        if not name:
            return None

        bare = name[3:] if name.startswith("wf:") else name
        for wf in cls._workflows(coder):
            if wf.name == bare:
                return wf

        return None

    @classmethod
    def _require(cls, io, coder, name, sub):
        """Resolve a required workflow-name argument or emit the error."""
        if not name:
            io.tool_error(f"Usage: /workflows {sub} <name>")
            return None

        wf = cls._find(coder, name)
        if wf is None:
            io.tool_error(cls._unknown_message(coder, name))

        return wf

    @classmethod
    def _unknown_message(cls, coder, name):
        known = ", ".join(sorted(wf.name for wf in cls._workflows(coder))) or "none loaded"
        return f"Error: unknown workflow '{name}'. Available: {known}"

    @staticmethod
    def _scheduler_db(coder):
        scheduler = getattr(coder, "scheduler", None)
        return getattr(scheduler, "db", None)

    @classmethod
    async def _job_rows(cls, coder, name=None):
        """jobs-table rows keyed by name (empty dict when no scheduler DB)."""
        db = cls._scheduler_db(coder)
        if db is None:
            return {}

        list_jobs = getattr(db, "list_jobs", None)
        if list_jobs is None:
            return {}

        try:
            rows = await list_jobs()
        except Exception:
            logger.warning("scheduler list_jobs failed", exc_info=True)
            return {}

        jobs = {}
        for row in rows or []:
            row = dict(row)
            key = row.get("name")
            if key and (name is None or key == name):
                jobs[key] = row

        return jobs

    @classmethod
    async def _active_leases(cls, coder, name=None):
        """Currently-held leases, if the scheduler DB exposes them."""
        db = cls._scheduler_db(coder)
        method = getattr(db, "active_leases", None) if db is not None else None
        if method is None:
            return []

        try:
            rows = await (method(name) if name else method())
        except TypeError:
            try:
                rows = await method()
            except Exception:
                logger.warning("scheduler active_leases failed", exc_info=True)
                return []
        except Exception:
            logger.warning("scheduler active_leases failed", exc_info=True)
            return []

        rows = [dict(r) for r in rows or []]
        return [r for r in rows if name is None or r.get("name") == name]

    @classmethod
    async def _recent_run(cls, coder, name):
        """The newest runs-table row for *name*, if a scheduler DB is present."""
        db = cls._scheduler_db(coder)
        recent_runs = getattr(db, "recent_runs", None) if db is not None else None
        if recent_runs is None:
            return None

        try:
            rows = await recent_runs(name, limit=1)
        except TypeError:
            try:
                rows = await recent_runs(name)
            except Exception:
                logger.warning("scheduler recent_runs failed", exc_info=True)
                return None
        except Exception:
            logger.warning("scheduler recent_runs failed", exc_info=True)
            return None

        return dict(rows[0]) if rows else None
