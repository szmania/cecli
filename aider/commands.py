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
from prompt_toolkit.keys import Keys
from prompt_toolkit.shortcuts import get_word_before_cursor

from aider import __version__, models, prompts, sessions, voice
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
        if not raw_completer:
            raw_completer = getattr(self, f"completions_{cmd}", None)

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

    def get_command_completions(self, document: Document):
        text = document.text.lstrip()
        if not text or text[0] not in "/!":
            return

        if " " not in text:
            command = text
            arg_text = ""
        else:
            command, arg_text = text.split(" ", 1)

        if command == "/add":
            path_completer = PathCompleter(
                get_paths=lambda: self.coder.get_addable_relative_files(),
                expanduser=True,
            )
            for comp in path_completer.get_completions(
                Document(arg_text, len(arg_text)),
                None,
            ):
                yield comp
            return

        if command == "/drop":
            path_completer = PathCompleter(
                get_paths=lambda: self.coder.get_inchat_relative_files(),
                expanduser=True,
            )
            for comp in path_completer.get_completions(
                Document(arg_text, len(arg_text)),
                None,
            ):
                yield comp
            return

        if command in ("/load-session", "/rm-session"):
            session_manager = self.get_session_manager()
            sessions = session_manager.list_sessions()
            word = get_word_before_cursor()
            for session in sessions:
                if session.startswith(word):
                    yield Completion(session, start_position=-len(word))
            return

        try:
            raw_completer = self.get_raw_completions(command)
            if raw_completer:
                for comp in raw_completer():
                    yield comp
        except CommandCompletionException:
            pass

    def matching_commands(self, inp):
        words = inp.strip().split()
        if not words:
            return

        first_word = words[0]
        rest_inp = inp[len(words[0]) :].strip()

        all_commands = self.get_commands()
        matching_commands = [cmd for cmd in all_commands if cmd.startswith(first_word)]
        return matching_commands, first_word, rest_inp

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

    async def run(self, inp):
        if not inp:
            return

        inp = inp.strip()

        if inp.startswith("!"):
            self.coder.event("command_run")
            return await self.do_run("run", inp[1:])

        if not inp.startswith("/"):
            return

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

    def cmd_ls_google(self, args):
        "List files and directories using google model"
        if not args:
            args = "."
        self.coder.abs_fnames.add(self.coder.abs_root_path(args))

    def quote_fname(self, fname):
        if " " in fname and '"' not in fname:
            fname = f'"{fname}"'
        return fname

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

    async def cmd_drop(self, args=""):
        "Remove files from the chat session to free up context space"

        try:
            if not args.strip():
                if self.original_read_only_fnames:
                    self.io.tool_output(
                        "Dropping all files from the chat session except originally read-only"
                        " files."
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
                    matched_files.append(expanded_word)

                for matched_file in matched_files:
                    abs_fname = self.coder.abs_root_path(matched_file)
                    if abs_fname in self.coder.abs_fnames:
                        self.coder.abs_fnames.remove(abs_fname)
                        self.io.tool_output(f"Removed {matched_file} from the chat")
                        files_changed = True

            # Recalculate context block tokens if any files were changed and using agent mode
            if (
                files_changed
                and hasattr(self.coder, "use_enhanced_context")
                and self.coder.use_enhanced_context
            ):
                if hasattr(self.coder, "_calculate_context_block_tokens"):
                    self.coder._calculate_context_block_tokens()
        finally:
            if self.coder.repo_map:
                map_tokens = self.coder.repo_map.max_map_tokens
                map_mul_no_files = self.coder.repo_map.map_mul_no_files
            else:
                map_tokens = 0
                map_mul_no_files = 1

            raise SwitchCoder(
                edit_format=self.coder.edit_format,
                summarize_from_coder=False,
                from_coder=self.coder,
                map_tokens=map_tokens,
                map_mul_no_files=map_mul_no_files,
                show_announcements=False,
            )

    def cmd_addable(self, args):
        "List files that can be added to the chat"
        files = self.coder.get_addable_relative_files()
        for file in files:
            self.io.tool_output(file)

    def _clear_chat_history(self):
        self.coder.done_messages = []
        self.coder.cur_messages = []

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

    def cmd_clear(self, args):
        "Clear the chat history"
        self._clear_chat_history()
        self.io.tool_output("Chat history cleared.")

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
                    content = f"{relative_fname}\n{fence}\n" + content + f"{fence}\n"
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
                    content = f"{relative_fname}\n{fence}\n" + content + f"{fence}\n"
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

    def cmd_history(self, args):
        "Show the chat history"
        self.io.tool_output(self.coder.io.chat_history_file)

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

    def cmd_git(self, args):
        "Run a git command"
        if not self.coder.repo:
            self.io.tool_error("Not in a git repo.")
            return

        try:
            result = self.coder.repo.repo.git.execute(args.split())
            self.io.tool_output(result)
        except ANY_GIT_ERROR as e:
            self.io.tool_error(str(e))

    async def cmd_commit(self, args):
        "Commit all pending changes"
        if not self.coder.repo:
            self.io.tool_error("Not in a git repo.")
            return

        if not self.coder.repo.is_dirty():
            self.io.tool_output("No changes to commit.")
            return

        commit_message = args.strip()
        if not commit_message:
            commit_message = await self.coder.repo.get_commit_message(
                "", self.coder.get_context_from_history(self.coder.cur_messages)
            )

        res = await self.coder.repo.commit(commit_message=commit_message, coder=self.coder)
        if res:
            self.coder.show_auto_commit_outcome(res)

    async def cmd_test(self, args):
        "Run a shell command and add the output to the chat on non-zero exit code"
        if not args and self.coder.test_cmd:
            args = self.coder.test_cmd

        if not args:
            return

        if not callable(args):
            if type(args) is not str:
                raise ValueError(repr(args))
            return await self.cmd_run(args, True)

        errors = args()
        if not errors:
            return

        self.io.tool_output(errors)
        return errors

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
            await self.coder.io.recreate_input()
            await lint_coder.run_one(errors, preproc=False)
            lint_coder.abs_fnames = set()

        if lint_coder and self.coder.repo.is_dirty() and self.coder.auto_commits:
            await self.cmd_commit("")

    async def cmd_run(self, args, add_on_nonzero_exit=False):
        "Run a shell command and optionally add the output to the chat (alias: !)"
        try:
            self.cmd_running = True
            exit_status, combined_output = await asyncio.to_thread(
                run_cmd,
                args,
                verbose=self.verbose,
                error_print=self.io.tool_error,
                cwd=self.coder.root,
            )
            self.cmd_running = False

            # This print statement, for whatever reason,
            # allows the thread to properly yield control of the terminal
            # to the main program
            print("")

            if combined_output is None:
                return

            # Calculate token count of output
            token_count = self.coder.main_model.token_count(combined_output)
            k_tokens = token_count / 1000

            if add_on_nonzero_exit:
                add = exit_status != 0
            else:
                add = await self.io.confirm_ask(
                    f"Add {k_tokens:.1f}k tokens of command output to the chat?"
                )

            if add:
                num_lines = len(combined_output.strip().splitlines())
                line_plural = "line" if num_lines == 1 else "lines"
                self.io.tool_output(f"Added {num_lines} {line_plural} of output to the chat.")

                msg = prompts.run_output.format(
                    command=args,
                    output=combined_output,
                )

                self.coder.cur_messages += [
                    dict(role="user", content=msg),
                    dict(role="assistant", content="Ok."),
                ]

                if add_on_nonzero_exit and exit_status != 0:
                    # Return the formatted output message for test failures
                    return msg
                elif add and exit_status != 0:
                    self.io.placeholder = "What's wrong? Fix"

            # Return None if output wasn't added or command succeeded
            return None
        finally:
            self.cmd_running = False

    def cmd_speak(self, args):
        "Speak the response aloud"
        if not self.voice:
            self.voice = voice.Voice(self.io, self.voice_language)
        self.voice.speak(args)

    def cmd_paste(self, args):
        """Paste image/text from the clipboard into the chat.\
        Optionally provide a name for the image."""
        try:
            # Check for image first
            image = ImageGrab.grabclipboard()
            if isinstance(image, Image.Image):
                if args.strip():
                    filename = args.strip()
                    ext = os.path.splitext(filename)[1].lower()
                    if ext in (".jpg", ".jpeg", ".png"):
                        basename = filename
                    else:
                        basename = f"{filename}.png"
                else:
                    basename = "clipboard_image.png"

                temp_dir = tempfile.mkdtemp()
                temp_file_path = os.path.join(temp_dir, basename)
                image_format = "PNG" if basename.lower().endswith(".png") else "JPEG"
                image.save(temp_file_path, image_format)

                abs_file_path = Path(temp_file_path).resolve()

                # Check if a file with the same name already exists in the chat
                existing_file = next(
                    (f for f in self.coder.abs_fnames if Path(f).name == abs_file_path.name), None
                )
                if existing_file:
                    self.coder.abs_fnames.remove(existing_file)
                    self.io.tool_output(f"Replaced existing image in the chat: {existing_file}")

                self.coder.abs_fnames.add(str(abs_file_path))
                self.io.tool_output(f"Added clipboard image to the chat: {abs_file_path}")
                self.coder.check_added_files()

                return

            # If not an image, try to get text
            text = pyperclip.paste()
            if text:
                self.io.tool_output(text)
                return text

            self.io.tool_error("No image or text content found in clipboard.")
            return

        except Exception as e:
            self.io.tool_error(f"Error processing clipboard content: {e}")

    def cmd_edit(self, args):
        "Edit a file in your editor"
        if not args:
            self.io.tool_error("Please specify a file to edit.")
            return

        fname = args.strip()
        abs_fname = self.coder.abs_root_path(fname)

        if not os.path.exists(abs_fname):
            self.io.tool_error(f"File {fname} does not exist.")
            return

        content = self.io.read_text(abs_fname)
        edited_content = pipe_editor(content, editor=self.editor)

        if content != edited_content:
            self.io.write_text(abs_fname, edited_content)
            self.io.tool_output(f"Updated {fname}")

    def cmd_ls_sessions(self, args):
        "List available sessions"
        session_manager = self.get_session_manager()
        sessions = session_manager.list_sessions()
        if not sessions:
            self.io.tool_output("No sessions found.")
        else:
            self.io.tool_output("Available sessions:")
            for session_name in sessions:
                self.io.tool_output(f"- {session_name}")

    def cmd_load_session(self, args):
        "Load a session"
        session_name = args.strip()
        if not session_name:
            self.io.tool_error("Please provide a session name to load.")
            return
        session_manager = self.get_session_manager()
        try:
            placeholder = session_manager.load_session(session_name)
            raise SwitchCoder(placeholder=placeholder)
        except FileNotFoundError:
            self.io.tool_error(f"Session '{session_name}' not found.")
        except Exception as e:
            self.io.tool_error(f"Error loading session: {e}")

    def cmd_save_session(self, args):
        "Save the current session"
        session_name = args.strip()
        if not session_name:
            self.io.tool_error("Please provide a name for the session.")
            return
        session_manager = self.get_session_manager()
        try:
            session_manager.save_session(session_name)
        except Exception as e:
            self.io.tool_error(f"Error saving session: {e}")

    def cmd_rm_session(self, args):
        "Remove a session"
        session_name = args.strip()
        if not session_name:
            self.io.tool_error("Please provide a session name to remove.")
            return
        session_manager = self.get_session_manager()
        try:
            session_manager.remove_session(session_name)
        except FileNotFoundError:
            self.io.tool_error(f"Session '{session_name}' not found.")
        except Exception as e:
            self.io.tool_error(f"Error removing session: {e}")

    def cmd_sessions(self, args):
        "Manage chat sessions"
        subcommand, *rest = (args.strip() + " ").split(" ", 1)
        rest = (rest[0] if rest else "").strip()

        if subcommand == "ls":
            return self.cmd_ls_sessions(rest)
        elif subcommand == "load":
            return self.cmd_load_session(rest)
        elif subcommand == "save":
            return self.cmd_save_session(rest)
        elif subcommand == "rm":
            return self.cmd_rm_session(rest)
        else:
            self.io.tool_error(
                "Unknown /sessions command. Use: /sessions <ls|load|save|rm> [name]"
            )

    def cmd_tree(self, args):
        "Display the repository file structure as a tree"
        if not self.coder.repo:
            self.io.tool_error("Not in a git repo.")
            return

        files = self.coder.get_all_relative_files()
        tree_string = self.make_tree(files)
        self.io.tool_output(tree_string)

    def make_tree(self, files):
        tree = {}
        for file in files:
            parts = file.split(os.sep)
            node = tree
            for part in parts:
                node = node.setdefault(part, {})
        return self.format_tree(tree)

    def format_tree(self, tree, indent=""):
        lines = []
        for i, (name, subtree) in enumerate(sorted(tree.items())):
            is_last = i == len(tree) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{indent}{connector}{name}")
            if subtree:
                new_indent = indent + ("    " if is_last else "│   ")
                lines.extend(self.format_tree(subtree, new_indent))
        return "\n".join(lines)

    def cmd_help(self, args):
        "Show help for commands"
        if not self.help:
            self.help = Help(self.io, self.parser)
        self.help.show_help(args)

    def cmd_info(self, args):
        "Show information about the current chat session"
        info = []
        info.append(f"Aider version: {__version__}")
        info.append(f"Model: {self.coder.main_model.name}")
        info.append(f"Edit format: {self.coder.edit_format}")
        if self.coder.repo:
            info.append(f"Git repo: {self.coder.repo.root}")
        else:
            info.append("Git repo: Not in a git repo.")
        info.append(f"Files in chat: {len(self.coder.abs_fnames)}")
        for fname in self.coder.get_inchat_relative_files():
            info.append(f"  - {fname}")
        self.io.tool_output("\n".join(info))

    def cmd_copy_context(self, args=None):
        """Copy the current chat context as markdown, suitable to paste into a web UI"""

        chunks = self.coder.format_chat_chunks()

        markdown = ""

        # Only include specified chunks in order
        for messages in [chunks.repo, chunks.readonly_files, chunks.chat_files]:
            for msg in messages:
                # Only include user messages
                if msg["role"] != "user":
                    continue

                content = msg["content"]

                # Handle image/multipart content
                if isinstance(content, list):
                    for part in content:
                        if part.get("type") == "text":
                            markdown += part["text"] + "\n\n"
                else:
                    markdown += content + "\n\n"

        args = args or ""
        markdown += f"""
Just tell me how to edit the files to make the changes.
Don't give me back entire files.
Just show me the edits I need to make.

{args}
"""

        try:
            pyperclip.copy(markdown)
            self.io.tool_output("Copied code context to clipboard.")
        except pyperclip.PyperclipException as e:
            self.io.tool_error(f"Failed to copy to clipboard: {str(e)}")
            self.io.tool_output(
                "You may need to install xclip or xsel on Linux, or pbcopy on macOS."
            )
        except Exception as e:
            self.io.tool_error(f"An unexpected error occurred while copying to clipboard: {str(e)}")

    def cmd_settings(self, args):
        "Print out the current settings"
        settings = format_settings(self.parser, self.args)
        announcements = "\n".join(self.coder.get_announcements())

        # Build metadata for the active models (main, editor, weak)
        model_sections = []
        active_models = [
            ("Main model", self.coder.main_model),
            ("Editor model", getattr(self.coder.main_model, "editor_model", None)),
            ("Weak model", getattr(self.coder.main_model, "weak_model", None)),
        ]
        for label, model in active_models:
            if not model:
                continue
            info = getattr(model, "info", {}) or {}
            if not info:
                continue
            model_sections.append(f"{label} ({model.name}):")
            for k, v in sorted(info.items()):
                model_sections.append(f"  {k}: {v}")
            model_sections.append("")  # blank line between models

        model_metadata = "\n".join(model_sections)

        output = f"{announcements}\n{settings}"
        if model_metadata:
            output += "\n" + model_metadata
        self.io.tool_output(output)

    def cmd_format(self, args):
        "Format a file using the repo's linting/formatting commands"
        if not args:
            fnames = self.coder.get_inchat_relative_files()
        else:
            fnames = args.split()

        if not fnames:
            self.io.tool_error("No files to format.")
            return

        for fname in fnames:
            if not os.path.exists(self.coder.abs_root_path(fname)):
                self.io.tool_error(f"File not found: {fname}")
                continue
            self.coder.linter.format(self.coder.abs_root_path(fname))
            self.io.tool_output(f"Formatted {fname}")

    def cmd_context_management(self, args=""):
        "Toggle context management for large files"
        if not hasattr(self.coder, "context_management_enabled"):
            self.io.tool_error("Context management is only available in agent mode.")
            return

        # Toggle the setting
        self.coder.context_management_enabled = not self.coder.context_management_enabled

        # Report the new state
        if self.coder.context_management_enabled:
            self.io.tool_output("Context management is now ON - large files may be truncated.")
        else:
            self.io.tool_output("Context management is now OFF - files will not be truncated.")

    def cmd_todo(self, args):
        "Manage the todo list"
        todo_file_path = ".aider.todo.txt"
        abs_path = self.coder.abs_root_path(todo_file_path)

        if not args:
            if os.path.exists(abs_path):
                content = self.io.read_text(abs_path)
                if content.strip():
                    self.io.tool_output("Current todo list:")
                    self.io.tool_output(content)
                else:
                    self.io.tool_output("Todo list is empty.")
            else:
                self.io.tool_output("Todo list is empty.")
            return

        subcommand, *rest = args.split(" ", 1)
        item = rest[0] if rest else ""

        if subcommand == "add":
            if not item:
                self.io.tool_error("Please provide an item to add to the todo list.")
                return
            with open(abs_path, "a") as f:
                f.write(f"- [ ] {item}\n")
            self.io.tool_output(f"Added '{item}' to the todo list.")
        elif subcommand == "rm":
            if not item:
                self.io.tool_error("Please provide an item to remove from the todo list.")
                return
            if not os.path.exists(abs_path):
                self.io.tool_error("Todo list is empty.")
                return
            lines = self.io.read_text(abs_path).splitlines()
            new_lines = [line for line in lines if item not in line]
            if len(lines) == len(new_lines):
                self.io.tool_error(f"Item '{item}' not found in the todo list.")
            else:
                self.io.write_text(abs_path, "\n".join(new_lines) + "\n")
                self.io.tool_output(f"Removed '{item}' from the todo list.")
        else:
            self.io.tool_error("Unknown /todo command. Use: /todo [add|rm] [item]")

    async def cmd_exit(self, args):
        "Exit the application"
        self.coder.event("exit", reason="/exit")

        for server in self.coder.mcp_servers:
            try:
                await server.exit_stack.aclose()
            except Exception:
                pass

        await asyncio.sleep(0)

        try:
            if self.coder.args.linear_output:
                os._exit(0)
            else:
                sys.exit()
        except Exception:
            sys.exit()

    async def cmd_quit(self, args):
        "Exit the application"
        await self.cmd_exit(args)

    def cmd_context_blocks(self, args=""):
        "Toggle enhanced context blocks or print a specific block"
        if not hasattr(self.coder, "use_enhanced_context"):
            self.io.tool_error("Enhanced context blocks are only available in agent mode.")
            return

        # If an argument is provided, try to print that specific context block
        if args.strip():
            # Format block name to match internal naming conventions
            block_name = args.strip().lower().replace(" ", "_")

            # Check if the coder has the necessary method to get context blocks
            if hasattr(self.coder, "_generate_context_block"):
                # Force token recalculation to ensure blocks are fresh
                if hasattr(self.coder, "_calculate_context_block_tokens"):
                    self.coder._calculate_context_block_tokens(force=True)

                # Try to get the requested block
                block_content = self.coder._generate_context_block(block_name)

                if block_content:
                    # Calculate token count
                    tokens = self.coder.main_model.token_count(block_content)
                    self.io.tool_output(f"Context block '{args.strip()}' ({tokens} tokens):")
                    self.io.tool_output(block_content)
                    return
                else:
                    # List available blocks if the requested one wasn't found
                    self.io.tool_error(f"Context block '{args.strip()}' not found or empty.")
                    if hasattr(self.coder, "context_block_tokens"):
                        available_blocks = list(self.coder.context_block_tokens.keys())
                        formatted_blocks = [
                            name.replace("_", " ").title() for name in available_blocks
                        ]
                        self.io.tool_output(f"Available blocks: {', '.join(formatted_blocks)}")
                    return
            else:
                self.io.tool_error("This coder doesn't support generating context blocks.")
                return

        # If no argument, toggle the enhanced context setting
        self.coder.use_enhanced_context = not self.coder.use_enhanced_context

        # Report the new state
        if self.coder.use_enhanced_context:
            self.io.tool_output(
                "Enhanced context blocks are now ON - directory structure and git status will be"
                " included."
            )
            if hasattr(self.coder, "context_block_tokens"):
                available_blocks = list(self.coder.context_block_tokens.keys())
                formatted_blocks = [name.replace("_", " ").title() for name in available_blocks]
                self.io.tool_output(f"Available blocks: {', '.join(formatted_blocks)}")
                self.io.tool_output("Use '/context-blocks [block name]' to view a specific block.")
        else:
            self.io.tool_output(
                "Enhanced context blocks are now OFF - directory structure and git status will not"
                " be included."
            )

    def cmd_ls(self, args):
        "List all known files and indicate which are included in the chat session"

        files = self.coder.get_all_relative_files()

        other_files = []
        chat_files = []
        read_only_files = []
        read_only_stub_files = []
        for file in files:
            abs_file_path = self.coder.abs_root_path(file)
            if abs_file_path in self.coder.abs_fnames:
                chat_files.append(file)
            else:
                other_files.append(file)

        # Add read-only files
        for abs_file_path in self.coder.abs_read_only_fnames:
            rel_file_path = self.coder.get_rel_fname(abs_file_path)
            read_only_files.append(rel_file_path)

        # Add read-only stub files
        for abs_file_path in self.coder.abs_read_only_stubs_fnames:
            rel_file_path = self.coder.get_rel_fname(abs_file_path)
            read_only_stub_files.append(rel_file_path)

        if not chat_files and not other_files and not read_only_files and not read_only_stub_files:
            self.io.tool_output("\nNo files in chat, git repo, or read-only list.")
            return

        if other_files:
            self.io.tool_output("Repo files not in the chat:\n")
        for file in other_files:
            self.io.tool_output(f"  {file}")

        # Read-only files:
        if read_only_files or read_only_stub_files:
            self.io.tool_output("\nRead-only files:\n")
        for file in read_only_files:
            self.io.tool_output(f"  {file}")
        for file in read_only_stub_files:
            self.io.tool_output(f"  {file} (stub)")

        if chat_files:
            self.io.tool_output("\nFiles in chat:\n")
        for file in chat_files:
            self.io.tool_output(f"  {file}")

    async def _generic_chat_command(self, args, edit_format, placeholder=None):
        if not args.strip():
            # Switch to the corresponding chat mode if no args provided
            return self.cmd_chat_mode(edit_format)

        from aider.coders.base_coder import Coder

        coder = await Coder.create(
            io=self.io,
            from_coder=self.coder,
            edit_format=edit_format,
            summarize_from_coder=False,
            num_cache_warming_pings=0,
            aider_commit_hashes=self.coder.aider_commit_hashes,
        )

        user_msg = args
        await coder.run(user_msg, False)
        self.coder.aider_commit_hashes = coder.aider_commit_hashes

    def get_help_md(self):
        "Show help about all commands in markdown"

        res = """
|Command|Description|
|:------|:----------|
"""
        commands = sorted(self.get_commands())
        for cmd in commands:
            cmd_method_name = f"cmd_{cmd[1:]}".replace("-", "_")
            cmd_method = getattr(self, cmd_method_name, None)
            if cmd_method:
                description = cmd_method.__doc__
                res += f"| **{cmd}** | {description} |\n"
            else:
                res += f"| **{cmd}** | |\n"

        res += "\n"
        return res

    def cmd_voice(self, args):
        "Record and transcribe voice input"

        if not self.voice:
            if "OPENAI_API_KEY" not in os.environ:
                self.io.tool_error("To use /voice you must provide an OpenAI API key.")
                return
            try:
                self.voice = voice.Voice(
                    audio_format=self.voice_format or "wav", device_name=self.voice_input_device
                )
            except voice.SoundDeviceError:
                self.io.tool_error(
                    "Unable to import `sounddevice` and/or `soundfile`, is portaudio installed?"
                )
                return

        try:
            text = self.voice.record_and_transcribe(None, language=self.voice_language)
        except litellm.OpenAIError as err:
            self.io.tool_error(f"Unable to use OpenAI whisper model: {err}")
            return

        if text:
            self.io.placeholder = text

    def _cmd_read_only_base(self, args, source_set, target_set, source_mode, target_mode):
        """Base implementation for read-only and read-only-stub commands"""
        if not args.strip():
            # Handle editable files
            for fname in list(self.coder.abs_fnames):
                self.coder.abs_fnames.remove(fname)
                target_set.add(fname)
                rel_fname = self.coder.get_rel_fname(fname)
                self.io.tool_output(f"Converted {rel_fname} from editable to {target_mode}")

            # Handle source set files if provided
            if source_set:
                for fname in list(source_set):
                    source_set.remove(fname)
                    target_set.add(fname)
                    rel_fname = self.coder.get_rel_fname(fname)
                    self.io.tool_output(
                        f"Converted {rel_fname} from {source_mode} to {target_mode}"
                    )
            return

        filenames = parse_quoted_filenames(args)
        all_paths = []

        # First collect all expanded paths
        for pattern in filenames:
            expanded_pattern = expanduser(pattern)
            path_obj = Path(expanded_pattern)
            is_abs = path_obj.is_absolute()
            if not is_abs:
                path_obj = Path(self.coder.root) / path_obj

            matches = []
            # Check for literal path existence first
            if path_obj.exists():
                matches = [path_obj]
            else:
                # If literal path doesn't exist, try globbing
                if is_abs:
                    # For absolute paths, glob it
                    matches = [Path(p) for p in glob.glob(expanded_pattern)]
                else:
                    # For relative paths and globs, use glob from the root directory
                    matches = list(Path(self.coder.root).glob(expanded_pattern))

            if not matches:
                self.io.tool_error(f"No matches found for: {pattern}")
            else:
                all_paths.extend(matches)

        # Then process them in sorted order
        for path in sorted(all_paths):
            abs_path = self.coder.abs_root_path(path)
            if os.path.isfile(abs_path):
                self._add_read_only_file(
                    abs_path,
                    path,
                    target_set,
                    source_set,
                    source_mode=source_mode,
                    target_mode=target_mode,
                )
            elif os.path.isdir(abs_path):
                self._add_read_only_directory(abs_path, path, source_set, target_set, target_mode)
            else:
                self.io.tool_error(f"Not a file or directory: {abs_path}")

    def _add_read_only_file(
        self,
        abs_path,
        original_name,
        target_set,
        source_set,
        source_mode="read-only",
        target_mode="read-only",
    ):
        if is_image_file(original_name) and not self.coder.main_model.info.get("supports_vision"):
            self.io.tool_error(
                f"Cannot add image file {original_name} as the"
                f" {self.coder.main_model.name} does not support images."
            )
            return

        if abs_path in target_set:
            self.io.tool_error(f"{original_name} is already in the chat as a {target_mode} file")
            return
        elif abs_path in self.coder.abs_fnames:
            self.coder.abs_fnames.remove(abs_path)
            target_set.add(abs_path)
            self.io.tool_output(
                f"Moved {original_name} from editable to {target_mode} files in the chat"
            )
        elif source_set and abs_path in source_set:
            source_set.remove(abs_path)
            target_set.add(abs_path)
            self.io.tool_output(
                f"Moved {original_name} from {source_mode} to {target_mode} files in the chat"
            )
        else:
            target_set.add(abs_path)
            self.io.tool_output(f"Added {original_name} to {target_mode} files.")

    def _add_read_only_directory(
        self, abs_path, original_name, source_set, target_set, target_mode
    ):
        added_files = 0
        for root, _, files in os.walk(abs_path):
            for file in files:
                file_path = os.path.join(root, file)
                if (
                    file_path not in self.coder.abs_fnames
                    and file_path not in target_set
                    and (source_set is None or file_path not in source_set)
                ):
                    target_set.add(file_path)
                    added_files += 1

        if added_files > 0:
            self.io.tool_output(
                f"Added {added_files} files from directory {original_name} to {target_mode} files."
            )
        else:
            self.io.tool_output(f"No new files added from directory {original_name}.")

    def cmd_read_only(self, args):
        "Add files to the chat that are for reference only, or turn added files to read-only"
        if not args.strip():
            # If no args provided, use fuzzy finder to select files to add as read-only
            all_files = self.coder.get_all_relative_files()
            files_in_chat = self.coder.get_inchat_relative_files()
            addable_files = sorted(set(all_files) - set(files_in_chat))
            if not addable_files:
                # If no files available to add, convert all editable files to read-only
                self._cmd_read_only_base(
                    "",
                    source_set=self.coder.abs_read_only_stubs_fnames,
                    target_set=self.coder.abs_read_only_fnames,
                    source_mode="read-only (stub)",
                    target_mode="read-only",
                )
                return
            selected_files = run_fzf(addable_files, multi=True)
            if not selected_files:
                # If user didn't select any files, convert all editable files to read-only
                self._cmd_read_only_base(
                    "",
                    source_set=self.coder.abs_read_only_stubs_fnames,
                    target_set=self.coder.abs_read_only_fnames,
                    source_mode="read-only (stub)",
                    target_mode="read-only",
                )
                return
            args = " ".join([self.quote_fname(f) for f in selected_files])

        self._cmd_read_only_base(
            args,
            source_set=self.coder.abs_read_only_stubs_fnames,
            target_set=self.coder.abs_read_only_fnames,
            source_mode="read-only (stub)",
            target_mode="read-only",
        )

    def cmd_read_only_stub(self, args):
        "Add files to the chat as read-only stubs, or turn added files to read-only (stubs)"
        if not args.strip():
            # If no args provided, use fuzzy finder to select files to add as read-only stubs
            all_files = self.coder.get_all_relative_files()
            files_in_chat = self.coder.get_inchat_relative_files()
            addable_files = sorted(set(all_files) - set(files_in_chat))
            if not addable_files:
                # If no files available to add, convert all editable files to read-only stubs
                self._cmd_read_only_base(
                    "",
                    source_set=self.coder.abs_read_only_fnames,
                    target_set=self.coder.abs_read_only_stubs_fnames,
                    source_mode="read-only",
                    target_mode="read-only (stub)",
                )
                return
            selected_files = run_fzf(addable_files, multi=True)
            if not selected_files:
                # If user didn't select any files, convert all editable files to read-only stubs
                self._cmd_read_only_base(
                    "",
                    source_set=self.coder.abs_read_only_fnames,
                    target_set=self.coder.abs_read_only_stubs_fnames,
                    source_mode="read-only",
                    target_mode="read-only (stub)",
                )
                return
            args = " ".join([self.quote_fname(f) for f in selected_files])

        self._cmd_read_only_base(
            args,
            source_set=self.coder.abs_read_only_fnames,
            target_set=self.coder.abs_read_only_stubs_fnames,
            source_mode="read-only",
            target_mode="read-only (stub)",
        )

    def cmd_map(self, args):
        "Print out the current repository map"
        repo_map = self.coder.get_repo_map()
        if repo_map:
            self.io.tool_output(repo_map)
        else:
            self.io.tool_output("No repository map available.")

    def cmd_map_refresh(self, args):
        "Force a refresh of the repository map"
        repo_map = self.coder.get_repo_map(force_refresh=True)
        if repo_map:
            self.io.tool_output("The repo map has been refreshed, use /map to view it.")

    def completions_raw_load(self, document, complete_event):
        return self.completions_raw_read_only(document, complete_event)

    async def cmd_load(self, args):
        "Load and execute commands from a file"
        if not args.strip():
            self.io.tool_error("Please provide a filename containing commands to load.")
            return

        try:
            with open(args.strip(), "r", encoding=self.io.encoding, errors="replace") as f:
                commands = f.readlines()
        except FileNotFoundError:
            self.io.tool_error(f"File not found: {args}")
            return
        except Exception as e:
            self.io.tool_error(f"Error reading file: {e}")
            return

        for cmd in commands:
            cmd = cmd.strip()
            if not cmd or cmd.startswith("#"):
                continue

            self.io.tool_output(f"\nExecuting: {cmd}")
            try:
                await self.run(cmd)
            except SwitchCoder:
                self.io.tool_error(
                    f"Command '{cmd}' is only supported in interactive mode, skipping."
                )

    def completions_raw_save(self, document, complete_event):
        return self.completions_raw_read_only(document, complete_event)

    def cmd_save(self, args):
        "Save commands to a file that can reconstruct the current chat session's files"
        if not args.strip():
            self.io.tool_error("Please provide a filename to save the commands to.")
            return

        try:
            with open(args.strip(), "w", encoding=self.io.encoding) as f:
                f.write("/drop\n")
                # Write commands to add editable files
                for fname in sorted(self.coder.abs_fnames):
                    rel_fname = self.coder.get_rel_fname(fname)
                    f.write(f"/add       {rel_fname}\n")

                # Write commands to add read-only files
                for fname in sorted(self.coder.abs_read_only_fnames):
                    # Use absolute path for files outside repo root, relative path for files inside
                    if Path(fname).is_relative_to(self.coder.root):
                        rel_fname = self.coder.get_rel_fname(fname)
                        f.write(f"/read-only {rel_fname}\n")
                    else:
                        f.write(f"/read-only {fname}\n")
                # Write commands to add read-only stubs files
                for fname in sorted(self.coder.abs_read_only_stubs_fnames):
                    # Use absolute path for files outside repo root, relative path for files inside
                    if Path(fname).is_relative_to(self.coder.root):
                        rel_fname = self.coder.get_rel_fname(fname)
                        f.write(f"/read-only-stub {rel_fname}\n")
                    else:
                        f.write(f"/read-only-stub {fname}\n")

            self.io.tool_output(f"Saved commands to {args.strip()}")
        except Exception as e:
            self.io.tool_error(f"Error saving commands to file: {e}")

    def cmd_multiline_mode(self, args):
        "Toggle multiline mode (swaps behavior of Enter and Meta+Enter)"
        self.io.toggle_multiline_mode()

    def cmd_copy(self, args):
        "Copy the last assistant message to the clipboard"
        all_messages = self.coder.done_messages + self.coder.cur_messages
        assistant_messages = [msg for msg in reversed(all_messages) if msg["role"] == "assistant"]

        if not assistant_messages:
            self.io.tool_error("No assistant messages found to copy.")
            return

        last_assistant_message = assistant_messages[0]["content"]

        try:
            pyperclip.copy(last_assistant_message)
            preview = (
                last_assistant_message[:50] + "..."
                if len(last_assistant_message) > 50
                else last_assistant_message
            )
            self.io.tool_output(f"Copied last assistant message to clipboard. Preview: {preview}")
        except pyperclip.PyperclipException as e:
            self.io.tool_error(f"Failed to copy to clipboard: {str(e)}")
            self.io.tool_output(
                "You may need to install xclip or xsel on Linux, or pbcopy on macOS."
            )
        except Exception as e:
            self.io.tool_error(f"An unexpected error occurred while copying to clipboard: {str(e)}")

    def cmd_report(self, args):
        "Report a problem by opening a GitHub Issue"
        from aider.report import report_github_issue

        announcements = "\n".join(self.coder.get_announcements())
        issue_text = announcements

        if args.strip():
            title = args.strip()
        else:
            title = None

        report_github_issue(issue_text, title=title, confirm=False)

    def cmd_editor(self, initial_content=""):
        "Open an editor to write a prompt"

        user_input = pipe_editor(initial_content, suffix="md", editor=self.editor)
        if user_input.strip():
            self.io.set_placeholder(user_input.rstrip())

    def cmd_history_search(self, args):
        "Fuzzy search in history and paste it in the prompt"
        history_lines = self.io.get_input_history()
        selected_lines = run_fzf(history_lines)
        if selected_lines:
            self.io.set_placeholder("".join(selected_lines))

    def cmd_think_tokens(self, args):
        """Set the thinking token budget, eg: 8096, 8k, 10.5k, 0.5M, or 0 to disable."""
        model = self.coder.main_model

        if not args.strip():
            # Display current value if no args are provided
            formatted_budget = model.get_thinking_tokens()
            if formatted_budget is None:
                self.io.tool_output("Thinking tokens are not currently set.")
            else:
                budget = model.get_raw_thinking_tokens()
                self.io.tool_output(
                    f"Current thinking token budget: {budget:,} tokens ({formatted_budget})."
                )
            return

        value = args.strip()
        model.set_thinking_tokens(value)

        # Handle the special case of 0 to disable thinking tokens
        if value == "0":
            self.io.tool_output("Thinking tokens disabled.")
        else:
            formatted_budget = model.get_thinking_tokens()
            budget = model.get_raw_thinking_tokens()
            self.io.tool_output(
                f"Set thinking token budget to {budget:,} tokens ({formatted_budget})."
            )

        self.io.tool_output()

        # Output announcements
        announcements = "\n".join(self.coder.get_announcements())
        self.io.tool_output(announcements)

    def cmd_reasoning_effort(self, args):
        "Set the reasoning effort level (values: number or low/medium/high depending on model)"
        model = self.coder.main_model

        if not args.strip():
            # Display current value if no args are provided
            reasoning_value = model.get_reasoning_effort()
            if reasoning_value is None:
                self.io.tool_output("Reasoning effort is not currently set.")
            else:
                self.io.tool_output(f"Current reasoning effort: {reasoning_value}")
            return

        value = args.strip()
        model.set_reasoning_effort(value)
        reasoning_value = model.get_reasoning_effort()
        self.io.tool_output(f"Set reasoning effort to {reasoning_value}")
        self.io.tool_output()

        # Output announcements
        announcements = "\n".join(self.coder.get_announcements())
        self.io.tool_output(announcements)


def expand_subdir(file_path):
    if file_path.is_file():
        yield file_path
        return

    if file_path.is_dir():
        for file in file_path.rglob("*"):
            if file.is_file():
                yield file


def parse_quoted_filenames(args):
    filenames = re.findall(r"\"(.+?)\"|(\S+)", args)
    filenames = [name for sublist in filenames for name in sublist if name]
    return filenames


def get_help_md():
    md = Commands(None, None).get_help_md()
    return md


def main():
    md = get_help_md()
    print(md)


if __name__ == "__main__":
    status = main()
    sys.exit(status)
