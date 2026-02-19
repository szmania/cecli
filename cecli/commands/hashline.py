from typing import List

from cecli.commands.utils.base_command import BaseCommand


class HashlineCommand(BaseCommand):
    NORM_NAME = "hashline"
    DESCRIPTION = (
        "Enter agent mode to autonomously discover and manage relevant files. If no prompt"
        " provided, switches to hashline mode."
    )

    @classmethod
    async def execute(cls, io, coder, args, **kwargs):
        """Execute the agent command with given parameters."""

        return await cls._generic_chat_command(
            io, coder, args, "hashline", placeholder=args.strip() or None
        )

    @classmethod
    def get_completions(cls, io, coder, args) -> List[str]:
        """Get completion options for agent command."""
        # The original completions_agent raises CommandCompletionException
        # This is handled by the completion system
        from cecli.io import CommandCompletionException

        raise CommandCompletionException()

    @classmethod
    def get_help(cls) -> str:
        """Get help text for the agent command."""
        help_text = super().get_help()
        help_text += "\nUsage:\n"
        help_text += "  /hashline <prompt>  # Enter hashline mode\n"
        help_text += "\nExamples:\n"
        help_text += (
            "  /hashline Fix this bug  # Use hashline edit format to autonomously fix a bug\n"
        )
        help_text += (
            "  /hashline Add a new feature  # Use hashline edit format to implement a feature\n"
        )
        return help_text
