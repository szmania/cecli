import asyncio
import glob
import importlib.resources
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from datetime import datetime
from os.path import expanduser
from pathlib import Path

import pyperclip
from PIL import Image, ImageGrab
from prompt_toolkit.completion import Completion, PathCompleter
from prompt_toolkit.document import Document

from aider import models, prompts, sessions, voice
from aider.editor import pipe_editor
from aider.format_settings import format_settings
from aider.help import Help, install_help_extra
from aider.io import CommandCompletionException
from aider.llm import litellm
from aider.repo import ANY_GIT_ERROR
from aider.run_cmd import run_cmd
from aider.scrape import Scraper, install_playwright
from aider.sessions import SessionManager
from aider.utils import is_image_file, run_fzf, strip_fenced_code

from .dump import dump  # noqa: F401


class SwitchCoder(Exception):
    def __init__(self, placeholder=None, **kwargs):
        self.kwargs = kwargs
        self.placeholder = placeholder


class Commands:
    voice = None
    scraper = None

    def clone(self):
        return Commands(
            self.io,
            None,
            voice_language=self.voice_language,
            verify_ssl=self.verify_ssl,
            args=self.args,
            parser=self.parser,
            verbose=self.verbose,
            editor=self.editor,
            original_read_only_fnames=self.original_read_only_fnames,
        )

    def __init__(
        self,
        io,
        coder,
        voice_language=None,
        voice_input_device=None,
        voice_format=None,
        verify_ssl=True,
        args=None,
        parser=None,
        verbose=False,
        editor=None,
        original_read_only_fnames=None,
    ):
        self.io = io
        self.coder = coder
        self.parser = parser
        self.args = args
        self.verbose = verbose

        self.verify_ssl = verify_ssl
        if voice_language == "auto":
            voice_language = None

        self.voice_language = voice_language
        self.voice_format = voice_format
        self.voice_input_device = voice_input_device

        self.help = None
        self.editor = editor

        # Store the original read-only filenames provided via args.read
        self.original_read_only_fnames = set(original_read_only_fnames or [])
        self.cmd_running = False
        self.session_manager = None

    def get_session_manager(self):
        if not self.session_manager:
            self.session_manager = SessionManager(self.coder, self.io)
        return self.session_manager

    async def cmd_model(self, args):
        "Switch the Main Model to a new LLM"

        model_name = args.strip()
        if not model_name:
            announcements = "\n".join(self.coder.get_announcements())
            self.io.tool_output(announcements)
            return

        model = models.Model(
            model_name,
            editor_model=self.coder.main_model.editor_model.name,
            weak_model=self.coder.main_model.weak_model.name,
        )
        await models.sanity_check_models(self.io, model)

        # Check if the current edit format is the default for the old model
        old_model_edit_format = self.coder.main_model.edit_format
        current_edit_format = self.coder.edit_format

        new_edit_format = current_edit_format
        if current_edit_format == old_model_edit_format:
            # If the user was using the old model's default, switch to the new model's default
            new_edit_format = model.edit_format

        raise SwitchCoder(main_model=model, edit_format=new_edit_format)

    async def cmd_editor_model(self, args):
        "Switch the Editor Model to a new LLM"

        model_name = args.strip()
        model = models.Model(
            self.coder.main_model.name,
            editor_model=model_name,
            weak_model=self.coder.main_model.weak_model.name,
        )
        await models.sanity_check_models(self.io, model)
        raise SwitchCoder(main_model=model)

    async def cmd_weak_model(self, args):
        "Switch the Weak Model to a new LLM"

        model_name = args.strip()
        model = models.Model(
            self.coder.main_model.name,
            editor_model=self.coder.main_model.editor_model.name,
            weak_model=model_name,
        )
        await models.sanity_check_models(self.io, model)
        raise SwitchCoder(main_model=model)

    def cmd_chat_mode(self, args):
        "Switch to a new chat mode"

        from aider import coders

        ef = args.strip()
        valid_formats = OrderedDict(
            sorted(
                (
                    coder.edit_format,
                    coder.__doc__.strip().split("\n")[0] if coder.__doc__ else "No description",
                )
                for coder in coders.__all__
                if getattr(coder, "edit_format", None)
            )
        )

        show_formats = OrderedDict(
            [
                ("help", "Get help about using aider (usage, config, troubleshoot)."),
                ("ask", "Ask questions about your code without making any changes."),
                ("code", "Ask for changes to your code (using the best edit format)."),
                (
                    "architect",
                    (
                        "Work with an architect model to design code changes, and an editor to make"
                        " them."
                    ),
                ),
                (
                    "context",
                    "Automatically identify which files will need to be edited.",
                ),
            ]
        )

        if ef not in valid_formats and ef not in show_formats:
            if ef:
                self.io.tool_error(f'Chat mode "{ef}" should be one of these:\n')
            else:
                self.io.tool_output("Chat mode should be one of these:\n")

            max_format_length = max(len(format) for format in valid_formats.keys())
            for format, description in show_formats.items():
                self.io.tool_output(f"- {format:<{max_format_length}} : {description}")

            self.io.tool_output("\nOr a valid edit format:\n")
            for format, description in valid_formats.items():
                if format not in show_formats:
                    self.io.tool_output(f"- {format:<{max_format_length}} : {description}")

            return

        summarize_from_coder = True
        edit_format = ef

        if ef == "code":
            edit_format = self.coder.main_model.edit_format
            summarize_from_coder = False
        elif ef == "ask":
            summarize_from_coder = False

        raise SwitchCoder(
            edit_format=edit_format,
            summarize_from_coder=summarize_from_coder,
        )

    def completions_model(self):
        models = litellm.model_cost.keys()
        return models

    def cmd_models(self, args):
        "Search the list of available models"

        args = args.strip()

        if args:
            models.print_matching_models(self.io, args)
        else:
            self.io.tool_output("Please provide a partial model name to search for.")

    async def cmd_web(self, args, return_content=False):
        "Scrape a webpage, convert to markdown and send in a message"

        url = args.strip()
        if not url:
            self.io.tool_error("Please provide a URL to scrape.")
            return

        self.io.tool_output(f"Scraping {url}...")
        if not self.scraper:
            disable_playwright = getattr(self.args, "disable_playwright", False)
            if disable_playwright:
                res = False
            else:
                try:
                    res = await install_playwright(self.io)
                    if not res:
                        self.io.tool_warning("Unable to initialize playwright.")
                except Exception:
                    self.io.tool_warning("Unable to initialize playwright.")
                    res = False

            self.scraper = Scraper(
                print_error=self.io.tool_error,
                playwright_available=res,
                verify_ssl=self.verify_ssl,
            )

        content = await self.scraper.scrape(url) or ""
        content = f"Here is the content of {url}:\n\n" + content
        if return_content:
            return content

        self.io.tool_output("... added to chat.")

        self.coder.cur_messages += [
            dict(role="user", content=content),
            dict(role="assistant", content="Ok."),
        ]

    def is_command(self, inp):
        return inp[0] in "/!"

    def is_run_command(self, inp):
        return inp and (inp[0] in "!" or inp[:5] == "/test" or inp[:4] == "/run")

    def get_raw_completions(self, cmd):
        assert cmd.startswith("/")
        cmd = cmd[1:]
        cmd = cmd.replace("-", "_")

        raw_completer = getattr(self, f"completions_raw_{cmd}", None)
        return raw_completer

    def get_completions(self, cmd):
        assert cmd.startswith("/")
        cmd = cmd[1:]

        cmd = cmd.replace("-", "_")
        fun = getattr(self, f"completions_{cmd}", None)
        if not fun:
            return
        return sorted(fun())

    def get_commands(self):
        commands = []
        for attr in dir(self):
            if not attr.startswith("cmd_"):
                continue
            cmd = attr[4:]
            cmd = cmd.replace("_", "-")
            commands.append("/" + cmd)

        return commands

    async def do_run(self, cmd_name, args):
        cmd_name = cmd_name.replace("-", "_")
        cmd_method_name = f"cmd_{cmd_name}"
        cmd_method = getattr(self, cmd_method_name, None)
        if not cmd_method:
            self.io.tool_output(f"Error: Command {cmd_name} not found.")
            return

        try:
            if asyncio.iscoroutinefunction(cmd_method):
                return await cmd_method(args)
            else:
                return cmd_method(args)
        except ANY_GIT_ERROR as err:
            self.io.tool_error(f"Unable to complete {cmd_name}: {err}")

    def matching_commands(self, inp):
        words = inp.strip().split()
        if not words:
            return

        first_word = words[0]
        rest_inp = inp[len(words[0]) :].strip()

        all_commands = self.get_commands()
        matching_commands = [cmd for cmd in all_commands if cmd.startswith(first_word)]
        return matching_commands, first_word, rest_inp

    async def run(self, inp):
        if inp.startswith("!"):
            self.coder.event("command_run")
            return await self.do_run("run", inp[1:])

        res = self.matching_commands(inp)
        if res is None:
            return
        matching_commands, first_word, rest_inp = res
        if len(matching_commands) == 1:
            command = matching_commands[0][1:]
            self.coder.event(f"command_{command}")
            return await self.do_run(command, rest_inp)
        elif first_word in matching_commands:
            command = first_word[1:]
            self.coder.event(f"command_{command}")
            return await self.do_run(command, rest_inp)
        elif len(matching_commands) > 1:
            self.io.tool_error(f"Ambiguous command: {', '.join(matching_commands)}")
        else:
            self.io.tool_error(f"Invalid command: {first_word}")

    # any method called cmd_xxx becomes a command automatically.
    # each one must take an args param.

    async def cmd_commit(self, args=None):
        "Commit edits to the repo made outside the chat (commit message optional)"
        try:
            await self.raw_cmd_commit(args)
        except ANY_GIT_ERROR as err:
            self.io.tool_error(f"Unable to complete commit: {err}")

    async def raw_cmd_commit(self, args=None):
        if not self.coder.repo:
            self.io.tool_error("No git repository found.")
            return

        if not self.coder.repo.is_dirty():
            self.io.tool_warning("No more changes to commit.")
            return

        commit_message = args.strip() if args else None
        await self.coder.repo.commit(message=commit_message, coder=self.coder)

    async def cmd_lint(self, args="", fnames=None):
        "Lint and fix in-chat files or all dirty files if none in chat"

        if not self.coder.repo:
            self.io.tool_error("No git repository found.")
            return

        if not fnames:
            fnames = self.coder.get_inchat_relative_files()

        # If still no files, get all dirty files in the repo
        if not fnames and self.coder.repo:
            fnames = self.coder.repo.get_dirty_files()

        if not fnames:
            self.io.tool_warning("No dirty files to lint.")
            return

        fnames = [self.coder.abs_root_path(fname) for fname in fnames]

        lint_coder = None
        for fname in fnames:
            try:
                errors = self.coder.linter.lint(fname)
            except FileNotFoundError as err:
                self.io.tool_error(f"Unable to lint {fname}")
                self.io.tool_output(str(err))
                continue

            if not errors:
                continue

            self.io.tool_output(errors)
            if not await self.io.confirm_ask(f"Fix lint errors in {fname}?", default="y"):
                continue

            # Commit everything before we start fixing lint errors
            if self.coder.repo.is_dirty() and self.coder.dirty_commits:
                await self.cmd_commit("")

            if not lint_coder:
                lint_coder = await self.coder.clone(
                    # Clear the chat history, fnames
                    cur_messages=[],
                    done_messages=[],
                    fnames=None,
                )

            lint_coder.add_rel_fname(fname)
            await lint_coder.run(errors)
            lint_coder.abs_fnames = set()

        if lint_coder and self.coder.repo.is_dirty() and self.coder.auto_commits:
            await self.cmd_commit("")

    def cmd_clear(self, args):
        "Clear the chat history"

        self._clear_chat_history()
        self.io.tool_output("All chat history cleared.")

    def _drop_all_files(self):
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_stubs_fnames = set()

        # When dropping all files, keep those that were originally provided via args.read
        if self.original_read_only_fnames:
            # Keep only the original read-only files
            to_keep = set()
            for abs_fname in self.coder.abs_read_only_fnames:
                rel_fname = self.coder.get_rel_fname(abs_fname)
                if (
                    abs_fname in self.original_read_only_fnames
                    or rel_fname in self.original_read_only_fnames
                ):
                    to_keep.add(abs_fname)
            self.coder.abs_read_only_fnames = to_keep
        else:
            self.coder.abs_read_only_fnames = set()

    def _clear_chat_history(self):
        self.coder.done_messages = []
        self.coder.cur_messages = []

    def cmd_reset(self, args):
        "Drop all files and clear the chat history"
        self._drop_all_files()
        self._clear_chat_history()
        self.io.tool_output("All files dropped and chat history cleared.")

    def cmd_tokens(self, args):
        "Report on the number of tokens used by the current chat context"

        res = []

        self.coder.choose_fence()

        # Show progress indicator
        total_files = len(self.coder.abs_fnames) + len(self.coder.abs_read_only_fnames)
        if total_files > 20:
            self.io.tool_output(f"Calculating tokens for {total_files} files...")

        # system messages
        main_sys = self.coder.fmt_system_prompt(self.coder.gpt_prompts.main_system)
        main_sys += "\n" + self.coder.fmt_system_prompt(self.coder.gpt_prompts.system_reminder)
        msgs = [
            dict(role="system", content=main_sys),
            dict(
                role="system",
                content=self.coder.fmt_system_prompt(self.coder.gpt_prompts.system_reminder),
            ),
        ]

        tokens = self.coder.main_model.token_count(msgs)
        res.append((tokens, "system messages", ""))

        # chat history
        msgs = self.coder.done_messages + self.coder.cur_messages
        if msgs:
            tokens = self.coder.main_model.token_count(msgs)
            res.append((tokens, "chat history", "use /clear to clear"))

        # repo map
        other_files = set(self.coder.get_all_abs_files()) - set(self.coder.abs_fnames)
        if self.coder.repo_map:
            repo_content = self.coder.repo_map.get_repo_map(self.coder.abs_fnames, other_files)
            if repo_content:
                tokens = self.coder.main_model.token_count(repo_content)
                res.append((tokens, "repository map", "use --map-tokens to resize"))

        # Enhanced context blocks (only for agent mode)
        if hasattr(self.coder, "use_enhanced_context") and self.coder.use_enhanced_context:
            # Force token calculation if it hasn't been done yet
            if hasattr(self.coder, "_calculate_context_block_tokens"):
                if not hasattr(self.coder, "tokens_calculated") or not self.coder.tokens_calculated:
                    self.coder._calculate_context_block_tokens()

            # Add enhanced context blocks to the display
            if hasattr(self.coder, "context_block_tokens") and self.coder.context_block_tokens:
                for block_name, tokens in self.coder.context_block_tokens.items():
                    # Format the block name more nicely
                    display_name = block_name.replace("_", " ").title()
                    res.append(
                        (tokens, f"{display_name} context block", "/context-blocks to toggle")
                    )

        fence = "`" * 3

        file_res = []
        # Process files with progress indication
        total_editable_files = len(self.coder.abs_fnames)
        total_readonly_files = len(self.coder.abs_read_only_fnames)

        # Display progress for editable files
        if total_editable_files > 0:
            if total_editable_files > 20:
                self.io.tool_output(
                    f"Calculating tokens for {total_editable_files} editable files..."
                )

            # Calculate tokens for editable files
            for i, fname in enumerate(self.coder.abs_fnames):
                if i > 0 and i % 20 == 0 and total_editable_files > 20:
                    self.io.tool_output(f"Processed {i}/{total_editable_files} editable files...")

                relative_fname = self.coder.get_rel_fname(fname)
                content = self.io.read_text(fname)
                if is_image_file(relative_fname):
                    tokens = self.coder.main_model.token_count_for_image(fname)
                else:
                    # approximate
                    content = f"{relative_fname}\n{fence}\n" + content + "{fence}\n"
                    tokens = self.coder.main_model.token_count(content)
                file_res.append((tokens, f"{relative_fname}", "/drop to remove"))

        # Display progress for read-only files
        if total_readonly_files > 0:
            if total_readonly_files > 20:
                self.io.tool_output(
                    f"Calculating tokens for {total_readonly_files} read-only files..."
                )

            # Calculate tokens for read-only files
            for i, fname in enumerate(self.coder.abs_read_only_fnames):
                if i > 0 and i % 20 == 0 and total_readonly_files > 20:
                    self.io.tool_output(f"Processed {i}/{total_readonly_files} read-only files...")

                relative_fname = self.coder.get_rel_fname(fname)
                content = self.io.read_text(fname)
                if content is not None and not is_image_file(relative_fname):
                    # approximate
                    content = f"{relative_fname}\n{fence}\n" + content + "{fence}\n"
                    tokens = self.coder.main_model.token_count(content)
                    file_res.append((tokens, f"{relative_fname} (read-only)", "/drop to remove"))

        if total_files > 20:
            self.io.tool_output("Token calculation complete. Generating report...")

        file_res.sort()
        res.extend(file_res)

        # stub files
        for fname in self.coder.abs_read_only_stubs_fnames:
            relative_fname = self.coder.get_rel_fname(fname)
            if not is_image_file(relative_fname):
                stub = self.coder.get_file_stub(fname)
                content = f"{relative_fname} (stub)\n{fence}\n" + stub + "{fence}\n"
                tokens = self.coder.main_model.token_count(content)
                res.append((tokens, f"{relative_fname} (read-only stub)", "/drop to remove"))

        self.io.tool_output(
            f"Approximate context window usage for {self.coder.main_model.name}, in tokens:"
        )
        self.io.tool_output()

        width = 8
        cost_width = 9

        def fmt(v):
            return format(int(v), ",").rjust(width)

        col_width = max(len(row[1]) for row in res) if res else 0

        cost_pad = " " * cost_width
        total = 0
        total_cost = 0.0
        for tk, msg, tip in res:
            total += tk
            cost = tk * (self.coder.main_model.info.get("input_cost_per_token") or 0)
            total_cost += cost
            msg = msg.ljust(col_width)
            self.io.tool_output(f"${cost:7.4f} {fmt(tk)} {msg} {tip}")  # noqa: E231

        self.io.tool_output("=" * (width + cost_width + 1))
        self.io.tool_output(f"${total_cost:7.4f} {fmt(total)} tokens total")  # noqa: E231

        limit = self.coder.main_model.info.get("max_input_tokens") or 0
        if not limit:
            return

        remaining = limit - total
        if remaining > 1024:
            self.io.tool_output(f"{cost_pad}{fmt(remaining)} tokens remaining in context window")
        elif remaining > 0:
            self.io.tool_error(
                f"{cost_pad}{fmt(remaining)} tokens remaining in context window (use /drop or"
                " /clear to make space)"
            )
        else:
            self.io.tool_error(
                f"{cost_pad}{fmt(remaining)} tokens remaining, window exhausted (use /drop or"
                " /clear to make space)"
            )
        self.io.tool_output(f"{cost_pad}{fmt(limit)} tokens max context window size")

    def cmd_undo(self, args):
        "Undo the last git commit if it was done by aider"
        try:
            self.raw_cmd_undo(args)
        except ANY_GIT_ERROR as err:
            self.io.tool_error(f"Unable to complete undo: {err}")

    def raw_cmd_undo(self, args):
        if not self.coder.repo:
            self.io.tool_error("No git repository found.")
            return

        last_commit = self.coder.repo.get_head_commit()
        if not last_commit or not last_commit.parents:
            self.io.tool_error("This is the first commit in the repository. Cannot undo.")
            return

        last_commit_hash = self.coder.repo.get_head_commit_sha(short=True)
        last_commit_message = self.coder.repo.get_head_commit_message("(unknown)").strip()
        last_commit_message = (last_commit_message.splitlines() or [""])[0]
        if last_commit_hash not in self.coder.aider_commit_hashes:
            self.io.tool_error("The last commit was not made by aider in this chat session.")
            self.io.tool_output(
                "You could try `/git reset --hard HEAD^` but be aware that this is a destructive"
                " command!"
            )
            return

        if len(last_commit.parents) > 1:
            self.io.tool_error(
                f"The last commit {last_commit.hexsha} has more than 1 parent, can't undo."
            )
            return

        prev_commit = last_commit.parents[0]
        changed_files_last_commit = [item.a_path for item in last_commit.diff(prev_commit)]

        for fname in changed_files_last_commit:
            if self.coder.repo.repo.is_dirty(path=fname):
                self.io.tool_error(
                    f"The file {fname} has uncommitted changes. Please stash them before undoing."
                )
                return

            # Check if the file was in the repo in the previous commit
            try:
                prev_commit.tree[fname]
            except KeyError:
                self.io.tool_error(
                    f"The file {fname} was not in the repository in the previous commit. Cannot"
                    " undo safely."
                )
                return

        local_head = self.coder.repo.repo.git.rev_parse("HEAD")
        current_branch = self.coder.repo.repo.active_branch.name
        try:
            remote_head = self.coder.repo.repo.git.rev_parse(f"origin/{current_branch}")
            has_origin = True
        except ANY_GIT_ERROR:
            has_origin = False

        if has_origin:
            if local_head == remote_head:
                self.io.tool_error(
                    "The last commit has already been pushed to the origin. Undoing is not"
                    " possible."
                )
                return

        # Reset only the files which are part of `last_commit`
        restored = set()
        unrestored = set()
        for file_path in changed_files_last_commit:
            try:
                self.coder.repo.repo.git.checkout("HEAD~1", file_path)
                restored.add(file_path)
            except ANY_GIT_ERROR:
                unrestored.add(file_path)

        if unrestored:
            self.io.tool_error(f"Error restoring {file_path}, aborting undo.")
            self.io.tool_output("Restored files:")
            for file in restored:
                self.io.tool_output(f"  {file}")
            self.io.tool_output("Unable to restore files:")
            for file in unrestored:
                self.io.tool_output(f"  {file}")
            return

        # Move the HEAD back before the latest commit
        self.coder.repo.repo.git.reset("--soft", "HEAD~1")

        self.io.tool_output(f"Removed: {last_commit_hash} {last_commit_message}")

        # Get the current HEAD after undo
        current_head_hash = self.coder.repo.get_head_commit_sha(short=True)
        current_head_message = self.coder.repo.get_head_commit_message("(unknown)").strip()
        current_head_message = (current_head_message.splitlines() or [""])[0]
        self.io.tool_output(f"Now at:  {current_head_hash} {current_head_message}")

        if self.coder.main_model.send_undo_reply:
            return prompts.undo_command_reply

    def cmd_diff(self, args=""):
        "Display the diff of changes since the last message"
        try:
            self.raw_cmd_diff(args)
        except ANY_GIT_ERROR as err:
            self.io.tool_error(f"Unable to complete diff: {err}")

    def raw_cmd_diff(self, args=""):
        if not self.coder.repo:
            self.io.tool_error("No git repository found.")
            return

        current_head = self.coder.repo.get_head_commit_sha()
        if current_head is None:
            self.io.tool_error("Unable to get current commit. The repository might be empty.")
            return

        if len(self.coder.commit_before_message) < 2:
            commit_before_message = current_head + "^"
        else:
            commit_before_message = self.coder.commit_before_message[-2]

        if not commit_before_message or commit_before_message == current_head:
            self.io.tool_warning("No changes to display since the last message.")
            return

        self.io.tool_output(f"Diff since {commit_before_message[:7]}...")

        if self.coder.pretty:
            run_cmd(f"git diff {commit_before_message}")
            return

        diff = self.coder.repo.diff_commits(
            self.coder.pretty,
            commit_before_message,
            "HEAD",
        )

        self.io.print(diff)

    def quote_fname(self, fname):
        if " " in fname and '"' not in fname:
            fname = f'"{fname}"'
        return fname

    def completions_raw_read_only_stub(self, document, complete_event):
        return self.completions_raw_read_only(document, complete_event)

    def completions_raw_read_only(self, document, complete_event):
        # Get the text before the cursor
        text = document.text_before_cursor

        # Skip the first word and the space after it
        after_command = text.split()[-1]

        # Create a new Document object with the text after the command
        new_document = Document(after_command, cursor_position=len(after_command))

        def get_paths():
            return [self.coder.root] if self.coder.root else None

        path_completer = PathCompleter(
            get_paths=get_paths,
            only_directories=False,
            expanduser=True,
        )

        # Adjust the start_position to replace all of 'after_command'
        adjusted_start_position = -len(after_command)

        # Collect all completions
        all_completions = []

        # Iterate over the completions and modify them
        for completion in path_completer.get_completions(new_document, complete_event):
            quoted_text = self.quote_fname(after_command + completion.text)
            all_completions.append(
                Completion(
                    text=quoted_text,
                    start_position=adjusted_start_position,
                    display=completion.display,
                    style=completion.style,
                    selected_style=completion.selected_style,
                )
            )

        # Add completions from the 'add' command
        add_completions = self.completions_add()
        for completion in add_completions:
            if after_command in completion:
                all_completions.append(
                    Completion(
                        text=completion,
                        start_position=adjusted_start_position,
                        display=completion,
                    )
                )

        # Sort all completions based on their text
        sorted_completions = sorted(all_completions, key=lambda c: c.text)

        # Yield the sorted completions
        for completion in sorted_completions:
            yield completion

    def completions_add(self):
        files = set(self.coder.get_all_relative_files())
        files = files - set(self.coder.get_inchat_relative_files())
        files = [self.quote_fname(fn) for fn in files]
        return files

    def glob_filtered_to_repo(self, pattern):
        if not pattern.strip():
            return []
        try:
            if os.path.isabs(pattern):
                # Handle absolute paths
                raw_matched_files = [Path(pattern)]
            else:
                try:
                    raw_matched_files = list(Path(self.coder.root).glob(pattern))
                except (IndexError, AttributeError):
                    raw_matched_files = []
        except ValueError as err:
            self.io.tool_error(f"Error matching {pattern}: {err}")
            raw_matched_files = []

        matched_files = []
        for fn in raw_matched_files:
            matched_files += expand_subdir(fn)

        matched_files = [
            fn.relative_to(self.coder.root)
            for fn in matched_files
            if fn.is_relative_to(self.coder.root)
        ]

        # if repo, filter against it
        if self.coder.repo:
            git_files = self.coder.repo.get_tracked_files()
            matched_files = [fn for fn in matched_files if str(fn) in git_files]

        res = list(map(str, matched_files))
        return res

    async def cmd_add(self, args):
        "Add files to the chat so aider can edit them or review them in detail"

        if not args.strip():
            all_files = self.coder.get_all_relative_files()
            files_in_chat = self.coder.get_inchat_relative_files()
            addable_files = sorted(set(all_files) - set(files_in_chat))
            if not addable_files:
                self.io.tool_output("No files available to add.")
                return
            selected_files = run_fzf(addable_files, multi=True)
            if not selected_files:
                return
            args = " ".join([self.quote_fname(f) for f in selected_files])

        all_matched_files = set()

        filenames = parse_quoted_filenames(args)
        for word in filenames:
            if Path(word).is_absolute():
                fname = Path(word)
            else:
                fname = Path(self.coder.root) / word

            if self.coder.repo and self.coder.repo.ignored_file(fname):
                self.io.tool_warning(f"Skipping {fname} due to aiderignore or --subtree-only.")
                continue

            if fname.exists():
                if fname.is_file():
                    all_matched_files.add(str(fname))
                    continue
                # an existing dir, escape any special chars so they won't be globs
                word = re.sub(r"([\*\?\[\]])", r"[\1]", word)

            matched_files = self.glob_filtered_to_repo(word)
            if matched_files:
                all_matched_files.update(matched_files)
                continue

            if "*" in str(fname) or "?" in str(fname):
                self.io.tool_error(
                    f"No match, and cannot create file with wildcard characters: {fname}"
                )
                continue

            if fname.exists() and fname.is_dir() and self.coder.repo:
                self.io.tool_error(f"Directory {fname} is not in git.")
                self.io.tool_output(f"You can add to git with: /git add {fname}")
                continue

            if await self.io.confirm_ask(
                f"No files matched '{word}'. Do you want to create {fname}?"
            ):
                try:
                    fname.parent.mkdir(parents=True, exist_ok=True)
                    fname.touch()
                    all_matched_files.add(str(fname))
                except OSError as e:
                    self.io.tool_error(f"Error creating file {fname}: {e}")

        for matched_file in sorted(all_matched_files):
            abs_file_path = self.coder.abs_root_path(matched_file)

            if not abs_file_path.startswith(self.coder.root) and not is_image_file(matched_file):
                self.io.tool_error(
                    f"Can not add {abs_file_path}, which is not within {self.coder.root}"
                )
                continue

            if (
                self.coder.repo
                and self.coder.repo.git_ignored_file(matched_file)
                and not self.coder.add_gitignore_files
            ):
                self.io.tool_error(f"Can't add {matched_file} which is in gitignore")
                continue

            if abs_file_path in self.coder.abs_fnames:
                self.io.tool_error(f"{matched_file} is already in the chat as an editable file")
                continue
            elif abs_file_path in self.coder.abs_read_only_stubs_fnames:
                if self.coder.repo and self.coder.repo.path_in_repo(matched_file):
                    self.coder.abs_read_only_stubs_fnames.remove(abs_file_path)
                    self.coder.abs_fnames.add(abs_file_path)
                    self.io.tool_output(
                        f"Moved {matched_file} from read-only (stub) to editable files in the chat"
                    )
                else:
                    self.io.tool_error(
                        f"Cannot add {matched_file} as it's not part of the repository"
                    )
            elif abs_file_path in self.coder.abs_read_only_fnames:
                if self.coder.repo and self.coder.repo.path_in_repo(matched_file):
                    self.coder.abs_read_only_fnames.remove(abs_file_path)
                    self.coder.abs_fnames.add(abs_file_path)
                    self.io.tool_output(
                        f"Moved {matched_file} from read-only to editable files in the chat"
                    )
                else:
                    self.io.tool_error(
                        f"Cannot add {matched_file} as it's not part of the repository"
                    )
            else:
                if is_image_file(matched_file) and not self.coder.main_model.info.get(
                    "supports_vision"
                ):
                    self.io.tool_error(
                        f"Cannot add image file {matched_file} as the"
                        f" {self.coder.main_model.name} does not support images."
                    )
                    continue
                content = self.io.read_text(abs_file_path)
                if content is None:
                    self.io.tool_error(f"Unable to read {matched_file}")
                else:
                    self.coder.abs_fnames.add(abs_file_path)
                    fname = self.coder.get_rel_fname(abs_file_path)
                    self.io.tool_output(f"Added {fname} to the chat")
                    self.coder.check_added_files()

                    # Recalculate context block tokens if using agent mode
                    if (
                        hasattr(self.coder, "use_enhanced_context")
                        and self.coder.use_enhanced_context
                    ):
                        if hasattr(self.coder, "_calculate_context_block_tokens"):
                            self.coder._calculate_context_block_tokens()

    def completions_drop(self):
        files = self.coder.get_inchat_relative_files()
        read_only_files = [
            self.coder.get_rel_fname(fn)
            for fn in self.coder.abs_read_only_fnames | self.coder.abs_read_only_stubs_fnames
        ]
        all_files = files + read_only_files
        all_files = [self.quote_fname(fn) for fn in all_files]
        return all_files

    def completions_context_blocks(self):
        """Return available context block names for auto-completion."""
        if not hasattr(self.coder, "use_enhanced_context") or not self.coder.use_enhanced_context:
            return []

        # If the coder has context blocks available
        if hasattr(self.coder, "context_block_tokens") and self.coder.context_block_tokens:
            # Get all block names from the tokens dictionary
            block_names = list(self.coder.context_block_tokens.keys())
            # Format them for display (convert snake_case to Title Case)
            formatted_blocks = [name.replace("_", " ").title() for name in block_names]
            return formatted_blocks

        # Standard blocks that are typically available
        return [
            "Context Summary",
            "Directory Structure",
            "Environment Info",
            "Git Status",
            "Symbol Outline",
        ]

    def _handle_read_only_files(self, expanded_word, file_set, description=""):
        """Handle read-only files with substring matching and samefile check"""
        matched = []
        for f in file_set:
            if expanded_word in f:
                matched.append(f)
                continue

            # Try samefile comparison for relative paths
            try:
                abs_word = os.path.abspath(expanded_word)
                if os.path.samefile(abs_word, f):
                    matched.append(f)
            except (FileNotFoundError, OSError):
                continue

        for matched_file in matched:
            file_set.remove(matched_file)
            self.io.tool_output(f"Removed {description} file {matched_file} from the chat")

    def cmd_drop(self, args=""):
        "Remove files from the chat session to free up context space"

        if not args.strip():
            if self.original_read_only_fnames:
                self.io.tool_output(
                    "Dropping all files from the chat session except originally read-only files."
                )
            else:
                self.io.tool_output("Dropping all files from the chat session.")
            self._drop_all_files()

            # Recalculate context block tokens after dropping all files
            if hasattr(self.coder, "use_enhanced_context") and self.coder.use_enhanced_context:
                if hasattr(self.coder, "_calculate_context_block_tokens"):
                    self.coder._calculate_context_block_tokens()
            return

        filenames = parse_quoted_filenames(args)
        files_changed = False

        for word in filenames:
            # Expand tilde in the path
            expanded_word = os.path.expanduser(word)

            # Handle read-only files
            self._handle_read_only_files(
                expanded_word, self.coder.abs_read_only_fnames, "read-only"
            )
            self._handle_read_only_files(
                expanded_word, self.coder.abs_read_only_stubs_fnames, "read-only (stub)"
            )

            # For editable files, use glob if word contains glob chars, otherwise use substring
            if any(c in expanded_word for c in "*?[]"):
                matched_files = self.glob_filtered_to_repo(expanded_word)
            else:
                # Use substring matching like we do for read-only files
                matched_files = [
                    self.coder.get_rel_fname(f)
                    for f in self.coder.abs_fnames
                    if expanded_word in f
                ]

            if not matched_files:
                self.io.tool_error(f"No files matched '{word}'")
                continue

            for matched_file in matched_files:
                abs_fname = self.coder.abs_root_path(matched_file)
                if abs_fname in self.coder.abs_fnames:
                    self.coder.abs_fnames.remove(abs_fname)
                    self.io.tool_output(f"Removed {matched_file} from the chat")
                    files_changed = True

        if files_changed:
            # Recalculate context block tokens if using agent mode
            if hasattr(self.coder, "use_enhanced_context") and self.coder.use_enhanced_context:
                if hasattr(self.coder, "_calculate_context_block_tokens"):
                    self.coder._calculate_context_block_tokens()

    def cmd_ls(self, args):
        "List files and directories, optionally with a pattern"
        if not args:
            args = "*"

        if self.coder.repo:
            files = self.coder.repo.get_tracked_files()
        else:
            files = [str(p) for p in Path(self.coder.root).rglob("*") if p.is_file()]

        in_chat = self.coder.get_inchat_relative_files()
        read_only_in_chat = [
            self.coder.get_rel_fname(fn)
            for fn in self.coder.abs_read_only_fnames | self.coder.abs_read_only_stubs_fnames
        ]

        try:
            patterns = parse_quoted_filenames(args)
            matched_files = set()
            for pattern in patterns:
                matched_files.update(fn for fn in files if glob.fnmatch.fnmatch(fn, pattern))
        except Exception as e:
            self.io.tool_error(f"Error matching pattern: {e}")
            return

        if not matched_files:
            self.io.tool_output("No files match the pattern.")
            return

        # Sort files: in-chat first, then alphabetically
        sorted_files = sorted(
            matched_files,
            key=lambda f: (f not in in_chat and f not in read_only_in_chat, f),
        )

        output = []
        for fname in sorted_files:
            if fname in in_chat:
                output.append(f"* {fname}")
            elif fname in read_only_in_chat:
                output.append(f"  {fname} (read-only)")
            else:
                output.append(f"  {fname}")

        self.io.tool_output("\n".join(output))

    def cmd_exit(self, args):
        "Exit the program"
        raise SystemExit

    def cmd_quit(self, args):
        "Exit the program"
        raise SystemExit

    def cmd_bye(self, args):
        "Exit the program"
        raise SystemExit

    def cmd_git(self, args):
        "Run a git command"
        if not self.coder.repo:
            self.io.tool_error("No git repository found.")
            return
        self.coder.repo.run_git_command(args)

    async def cmd_test(self, args):
        "Run a test command"
        if not args:
            args = self.coder.test_cmd

        if not args:
            self.io.tool_error("No test command provided.")
            return

        self.io.tool_output(f"Running test: {args}")
        self.io.tool_output(await run_cmd(args))

    async def cmd_run(self, args):
        "Run a shell command"
        self.io.tool_output(f"Running: {args}")
        self.io.tool_output(await run_cmd(args))

    async def cmd_help(self, args):
        "Show help about a command"
        if not self.help:
            self.help = Help(self.get_commands(), self.io)

        if not args:
            self.help.show_help()
            return

        if not args.startswith("/"):
            args = "/" + args

        if args not in self.get_commands():
            self.io.tool_error(f"unknown command: {args}")
            return

        self.help.show_command_help(args)

        if args == "/help":
            await install_help_extra(self.io)

    def cmd_info(self, args):
        "Show system information"
        self.io.tool_output(format_settings(self.parser, self.args))

    def cmd_save_session(self, args):
        "Save the chat session to a file"
        self.get_session_manager().save_session(args)

    def cmd_list_sessions(self, args):
        "List available chat sessions"
        self.get_session_manager().list_sessions()

    def cmd_load_session(self, args):
        "Load a chat session from a file"
        self.get_session_manager().load_session(args)

    def cmd_toggle_multiline(self, args):
        "Toggle multiline input mode"
        self.io.toggle_multiline_mode()

    def cmd_speak(self, args):
        "Speak the given text aloud"
        if not self.voice:
            self.voice = voice.Voice(self.io, self.voice_language)
        self.voice.speak(args)

    def cmd_listen(self, args):
        "Listen for voice input"
        if not self.voice:
            self.voice = voice.Voice(
                self.io, self.voice_language, self.voice_input_device, self.voice_format
            )
        return self.voice.listen()

    def cmd_paste(self, args):
        "Paste from the clipboard"
        return pyperclip.paste()

    def cmd_screenshot(self, args):
        "Add a screenshot to the chat"
        im = ImageGrab.grab()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fp:
            im.save(fp.name)
            self.cmd_add(fp.name)

    def cmd_agent(self, args):
        "Switch to the agent coder"
        raise SwitchCoder(edit_format="agent")

    def cmd_tools_create(self, args):
        "Create a new tool with AI assistance"

        scope = "local"
        args_list = args.strip().split()
        if "--global" in args_list:
            scope = "global"
            args_list.remove("--global")
        elif "--local" in args_list:
            scope = "local"
            args_list.remove("--local")

        file_name = args_list[0] if args_list else None
        description = " ".join(args_list[1:]) if len(args_list) > 1 else None

        if not file_name or not description:
            self.io.tool_error(
                "Usage: /tools-create [--local|--global] <file_name.py> <description>"
            )
            return

        if not hasattr(self.coder, "tool_create"):
            self.io.tool_error("This command is only available in agent mode.")
            return

        self.coder.tool_create(file_name=file_name, description=description, scope=scope)

    def cmd_tools_move(self, args):
        "Move a custom tool between the local and global tool directories"
        paths_str = args.strip()
        if not paths_str:
            self.io.tool_error(
                "Please provide the path(s) to Python files or directories containing tools to"
                " move."
            )
            return

        if not hasattr(self.coder, "tool_move"):
            self.io.tool_error("This command is only available in agent mode.")
            return

        self.coder.tool_move(paths_str)

    def cmd_tools_add(self, args):
        "Add a tool to the agent's tool belt"
        if not hasattr(self.coder, "tool_add_from_path"):
            self.io.tool_error("This command is only available in agent mode.")
            return
        self.coder.tool_add_from_path(args)

    def cmd_tools_remove(self, args):
        "Remove a tool from the agent's tool belt"
        if not hasattr(self.coder, "tool_remove"):
            self.io.tool_error("This command is only available in agent mode.")
            return
        self.coder.tool_remove(args)

    def cmd_tools_list(self, args):
        "List the tools in the agent's tool belt"
        if not hasattr(self.coder, "tool_list"):
            self.io.tool_error("This command is only available in agent mode.")
            return
        self.coder.tool_list()

    def cmd_granular_editing(self, args):
        "Toggle granular editing mode"
        if not hasattr(self.coder, "toggle_granular_editing"):
            self.io.tool_error("This command is only available in agent mode.")
            return
        self.coder.toggle_granular_editing()


def expand_subdir(path):
    if not path.is_dir():
        return [path]

    res = []
    for p in path.rglob("*"):
        if p.is_file():
            res.append(p)
    return res


def parse_quoted_filenames(args):
    res = []
    if not args:
        return res

    # Find all quoted strings
    quoted_strings = re.findall(r'"([^"]*)"', args)

    # Remove quoted strings from the original string to get unquoted words
    unquoted_part = re.sub(r'"[^"]*"', "", args)

    # Split the remaining string by spaces to get unquoted words
    unquoted_words = unquoted_part.split()

    # Combine quoted strings and unquoted words
    res.extend(quoted_strings)
    res.extend(unquoted_words)

    return res
