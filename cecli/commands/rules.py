import os
import re
from pathlib import Path
from typing import List

from cecli.commands.utils.base_command import BaseCommand
from cecli.commands.utils.helpers import (
    format_command_result,
    get_file_completions,
    parse_quoted_filenames,
    quote_filename,
)
from cecli.utils import run_fzf


class RulesCommand(BaseCommand):
    NORM_NAME = "rules"
    DESCRIPTION = "Add rule files to the chat for reference"

    @classmethod
    async def execute(cls, io, coder, args, **kwargs):
        """Execute the rules command with given parameters."""
        if not args.strip():
            rules_in_chat = [coder.get_rel_fname(f) for f in coder.abs_rules_fnames]
            if rules_in_chat:
                io.tool_output("Current rules files:")
                for rule_file in sorted(rules_in_chat):
                    io.tool_output(f"  {rule_file}")
                io.tool_output()

            all_files = coder.get_all_relative_files()
            # For rules, we might want to allow adding files already in chat as editable/read-only
            # but let's keep it simple and filter out what's already in abs_rules_fnames
            addable_files = sorted(set(all_files) - set(rules_in_chat))

            if not addable_files:
                if not rules_in_chat:
                    io.tool_output("No files available to add as rules.")
                return format_command_result(
                    io, "rules", "Listed rules" if rules_in_chat else "No files available to add"
                )

            selected_files = run_fzf(addable_files, multi=True, coder=coder)
            if not selected_files:
                return format_command_result(
                    io, "rules", "Listed rules" if rules_in_chat else "No files selected"
                )
            args = " ".join([quote_filename(f) for f in selected_files])

        all_matched_files = set()
        filenames = parse_quoted_filenames(args)

        for word in filenames:
            if Path(word).is_absolute():
                fname = Path(word)
            else:
                fname = Path(coder.root) / word

            if fname.exists():
                if fname.is_file():
                    all_matched_files.add(str(fname))
                    continue
                # an existing dir, escape any special chars so they won't be globs
                word = re.sub(r"([\*\?\[\]])", r"[\1]", word)

            matched_files = cls.glob_files(coder, word)
            if matched_files:
                all_matched_files.update(matched_files)
                continue

            if "*" in str(fname) or "?" in str(fname):
                io.tool_error(f"No match for wildcard pattern: {word}")
                continue

            io.tool_error(f"File or directory does not exist: {word}")

        for matched_file in sorted(all_matched_files):
            abs_file_path = coder.abs_root_path(matched_file)

            if abs_file_path in coder.abs_rules_fnames:
                io.tool_error(f"{matched_file} is already in the chat as a rules file")
                continue

            # Rules can be added regardless of whether they are in other sets
            coder.abs_rules_fnames.add(abs_file_path)
            fname = coder.get_rel_fname(abs_file_path)
            io.tool_output(f"Added {fname} to rules")

        return format_command_result(io, "rules", "Processed rules files")

    @classmethod
    def glob_files(cls, coder, pattern: str) -> List[str]:
        """Glob pattern and return all matching files."""
        if not pattern.strip():
            return []

        try:
            if os.path.isabs(pattern):
                raw_matched_files = [Path(pattern)]
            else:
                try:
                    raw_matched_files = list(Path(coder.root).glob(pattern))
                except (IndexError, AttributeError):
                    raw_matched_files = []
        except ValueError:
            raw_matched_files = []

        matched_files = []
        for fn in raw_matched_files:
            if fn.is_file():
                matched_files.append(fn)
            elif fn.is_dir():
                for f in fn.rglob("*"):
                    if f.is_file():
                        matched_files.append(f)

        return [str(fn) for fn in matched_files]

    @classmethod
    def get_completions(cls, io, coder, args) -> List[str]:
        """Get completion options for rules command."""
        directory_completions = get_file_completions(
            coder,
            args=args,
            completion_type="directory",
            include_directories=True,
            filter_in_chat=False,
        )

        all_completions = get_file_completions(
            coder, args=args, completion_type="all", include_directories=False, filter_in_chat=False
        )

        joint_set = set(directory_completions) | set(all_completions)
        return sorted(joint_set)

    @classmethod
    def get_help(cls) -> str:
        """Get help text for the rules command."""
        help_text = super().get_help()
        help_text += "\nUsage:\n"
        help_text += "  /rules              # Interactive file selection using fuzzy finder\n"
        help_text += "  /rules <files>      # Add specific files or glob patterns as rules\n"
        help_text += "\nExamples:\n"
        help_text += "  /rules .cursorrules # Add .cursorrules as a rule file\n"
        help_text += "  /rules rules/*.md   # Add all markdown files in rules directory\n"
        help_text += "\nThis command adds files to the chat as rules for reference.\n"
        return help_text
