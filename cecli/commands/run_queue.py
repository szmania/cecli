"""Run-queue command for CLI-33: processes queued prompts on demand.

Primary use: after cancelling an in-flight response (Ctrl+C), the in-flight
prompt is re-queued at the front. /run-queue resumes processing the queue.
"""

import asyncio
from typing import List

from cecli.commands.utils.base_command import BaseCommand
from cecli.commands.utils.helpers import format_command_result
from cecli.helpers import command_queue
from cecli.signals import SwitchCoderSignal


class RunQueueCommand(BaseCommand):
    NORM_NAME = "run-queue"
    DESCRIPTION = "Process queued prompts in order"

    @classmethod
    async def execute(cls, io, coder, args, **kwargs):
        """Execute the run-queue command with given parameters.

        Args:
            io: InputOutput instance
            coder: Coder instance (may be None for some commands)
            args: Command arguments (unused for run-queue)
            **kwargs: Additional context

        Returns:
            Formatted result string
        """
        # Sad path: coder.commands is None
        if not coder.commands:
            return format_command_result(
                io, cls.NORM_NAME, "", error="Command system not available. Cannot run queue."
            )

        # Resolve the target coder (sub-agent aware: use the foreground agent's queue)
        target = command_queue.get_active_coder(coder)

        # Sad path: empty queue
        if command_queue.get_queue_length(target) == 0:
            io.tool_output("Queue is empty. Use /queue <prompt> to add prompts first.")
            return f"Successfully executed {cls.NORM_NAME}."

        # Happy path: drain the queue
        try:
            await target.process_prompt_queue()
            return f"Successfully executed {cls.NORM_NAME}."
        except (KeyboardInterrupt, asyncio.CancelledError):
            # The interrupted item was re-queued; let the user know the queue paused.
            io.tool_output("Queue processing paused. Use /run-queue to resume.")
            return f"Successfully executed {cls.NORM_NAME}."
        except (SwitchCoderSignal, SystemExit):
            raise
        except Exception as e:
            return format_command_result(io, cls.NORM_NAME, "", error=str(e))

    @classmethod
    def get_completions(cls, io, coder, args) -> List[str]:
        """Get completion options for run-queue command."""
        return []

    @classmethod
    def get_help(cls) -> str:
        """Get help text for the run-queue command."""
        help_text = super().get_help()
        help_text += "\nUsage:\n"
        help_text += "  /run-queue  # Process all queued prompts in FIFO order\n"
        help_text += "\nDescription:\n"
        help_text += "  Processes every prompt currently in the queue, in order.\n"
        help_text += "  Queued prompts normally run automatically after the current\n"
        help_text += "  response finishes. Use /run-queue to resume a queue that was\n"
        help_text += "  paused (e.g. after cancelling an in-flight response with Ctrl+C).\n"
        help_text += "\nExamples:\n"
        help_text += "  /run-queue  # Drains the queue, processing each prompt in order\n"
        help_text += "\nSee also: /queue, /insert-queue, /list-queue, /remove-queue\n"
        return help_text
