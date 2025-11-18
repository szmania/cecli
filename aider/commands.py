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

        raw_completer = getattr(self, f"completions_{cmd}", None)
        if not raw_completer:
            return

        completions = raw_completer()
        if not completions:
            return

        word = get_word_before_cursor()
        for comp in completions:
            if comp.lower().startswith(word.lower()):
                yield Completion(comp, start_position=-len(word))

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
            for comp in self.get_raw_completions(command):
                yield comp
        except CommandCompletionException:
            pass

    def cmd_ls(self, args):
        "List files and directories"
        if not args:
            args = "."
        try:
            process = subprocess.run(
                ["ls", "-F", args],
                capture_output=True,
                text=True,
                check=True,
                cwd=self.coder.root,
            )
            self.io.tool_output(process.stdout)
        except FileNotFoundError:
            self.io.tool_error("Unable to run `ls`, is it in your PATH?")
        except subprocess.CalledProcessError as e:
            self.io.tool_error(e.stderr)

    def cmd_ls_google(self, args):
        "List files and directories using google model"
        if not args:
            args = "."
        self.coder.abs_fnames.add(self.coder.abs_root_path(args))

    def cmd_add(self, args):
        "Add files to the chat"
        for word in args.split():
            if word == ".":
                self.io.tool_error('Use "/add ." to add all files in the current git repo.')
                continue

            files = glob.glob(word, recursive=True)
            if not files:
                self.io.tool_error(f"No files matched '{word}'")
                continue

            for file in files:
                abs_file_path = self.coder.abs_root_path(file)
                if not os.path.exists(abs_file_path):
                    self.io.tool_error(f"File not found: {file}")
                    continue
                if os.path.isdir(abs_file_path):
                    self.io.tool_error(f"Can not add directory: {file}")
                    continue

                self.coder.add_rel_fname(file)
                self.io.tool_output(f"Added {file} to the chat.")

    def cmd_drop(self, args):
        "Remove files from the chat"
        for word in args.split():
            files = glob.glob(word, recursive=True)
            if not files:
                self.io.tool_error(f"No files matched '{word}'")
                continue

            for file in files:
                if self.coder.drop_rel_fname(file):
                    self.io.tool_output(f"Removed {file} from the chat.")
                else:
                    self.io.tool_error(f"File not in chat: {file}")

    def cmd_addable(self, args):
        "List files that can be added to the chat"
        files = self.coder.get_addable_relative_files()
        for file in files:
            self.io.tool_output(file)

    def cmd_clear(self, args):
        "Clear the chat history"
        self.coder.done_messages = []
        self.io.tool_output("Chat history cleared.")

    def cmd_tokens(self, args):
        "Report on the number of tokens used in the current chat context"
        self.io.tool_output(self.coder.format_messages().token_report())

    def cmd_history(self, args):
        "Show the chat history"
        self.io.tool_output(self.coder.io.chat_history_file)

    def cmd_diff(self, args=""):
        "Display the diff of the last aider commit"
        if not self.coder.repo:
            self.io.tool_error("Not in a git repo.")
            return

        if not self.coder.last_aider_commit_hash:
            self.io.tool_error("No previous aider commit.")
            return

        args = args.strip()
        if args:
            self.io.tool_error("Unexpected arguments to /diff")
            return

        diff = self.coder.repo.diff_commits(
            self.pretty,
            "HEAD",
            self.coder.last_aider_commit_hash,
        )
        if not diff:
            self.io.tool_output("No changes in last aider commit.")
            return

        self.io.tool_output(diff)

    def cmd_undo(self, args):
        "Undo the last aider commit"
        if not self.coder.repo:
            self.io.tool_error("Not in a git repo.")
            return

        if not self.coder.last_aider_commit_hash:
            self.io.tool_error("No previous aider commit to undo.")
            return

        if self.coder.repo.is_dirty():
            self.io.tool_error(
                "Your git repo is dirty. Please commit or stash your changes before undoing."
            )
            return

        head_hash = self.coder.repo.get_head_commit_sha()
        if head_hash != self.coder.last_aider_commit_hash:
            self.io.tool_error("The last commit is not an aider commit.")
            return

        self.coder.repo.repo.git.reset("--hard", "HEAD^")
        self.io.tool_output(f"Reset to {head_hash[:7]}^")
        self.coder.last_aider_commit_hash = None

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
        "Run the repo's test command"
        if not args:
            args = self.coder.test_cmd

        if not args:
            self.io.tool_error("No test command provided.")
            return

        self.io.tool_output(f"Running test command: {args}")
        self.io.tool_output("=" * 50)
        p = await asyncio.create_subprocess_shell(
            args,
            cwd=self.coder.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await p.communicate()
        self.io.tool_output("=" * 50)

        if stdout:
            self.io.tool_output(stdout.decode())
        if stderr:
            self.io.tool_error(stderr.decode())

        if p.returncode == 0:
            self.io.tool_output("Tests passed.")
            return ""
        else:
            err = f"Tests failed with exit code {p.returncode}."
            self.io.tool_error(err)
            return err

    def cmd_lint(self, args):
        "Run the repo's lint command"
        fnames = self.coder.get_inchat_relative_files()
        if not fnames:
            self.io.tool_error("No files to lint.")
            return

        self.coder.lint_edited(fnames)

    def cmd_run(self, args):
        "Run a shell command"
        res = run_cmd(args, self.coder.root, self.io.tool_output, self.io.tool_error)
        self.cmd_running = False
        return res

    def cmd_speak(self, args):
        "Speak the response aloud"
        if not self.voice:
            self.voice = voice.Voice(self.io, self.voice_language)
        self.voice.speak(args)

    def cmd_paste(self, args):
        "Paste from the clipboard"
        if sys.platform == "darwin":
            im = ImageGrab.grabclipboard()
            if im:
                temp_img = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                im.save(temp_img.name)
                self.coder.add_rel_fname(temp_img.name)
                self.io.tool_output(f"Added {temp_img.name} to the chat.")
                return

        content = pyperclip.paste()
        if not content:
            self.io.tool_error("Clipboard is empty.")
            return

        if not args:
            args = "pasted.txt"

        fname = self.coder.abs_root_path(args)
        if os.path.exists(fname):
            self.io.tool_error(f"File {args} already exists.")
            return

        self.io.write_text(fname, content)
        self.coder.add_rel_fname(args)
        self.io.tool_output(f"Pasted clipboard into {args} and added to chat.")

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

    def cmd_fzf(self, args):
        "Add files to the chat using fzf"
        addable_files = self.coder.get_addable_relative_files()
        if not addable_files:
            self.io.tool_error("No files available to add.")
            return

        try:
            selected_files = run_fzf(addable_files)
            for file in selected_files:
                self.coder.add_rel_fname(file)
                self.io.tool_output(f"Added {file} to the chat.")
        except FileNotFoundError:
            self.io.tool_error("fzf is not installed. Please install it to use this command.")
        except Exception as e:
            self.io.tool_error(f"An error occurred while running fzf: {e}")

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

    def cmd_copy_context(self, args=""):
        "Copy the conversation context to the clipboard"
        context = self.coder.format_messages().token_report(with_files=True)
        pyperclip.copy(context)
        self.io.tool_output("Copied conversation context to clipboard.")

    def cmd_config(self, args):
        "Show the project configuration"
        settings = format_settings(self.args)
        self.io.tool_output(settings)

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

    def cmd_context_management(self, args):
        "Toggle context management on or off"
        current_status = "enabled" if self.coder.context_management_enabled else "disabled"
        self.io.tool_output(f"Context management is currently {current_status}.")

        if args.lower() == "on":
            self.coder.context_management_enabled = True
            self.io.tool_output("Context management has been enabled.")
        elif args.lower() == "off":
            self.coder.context_management_enabled = False
            self.io.tool_output("Context management has been disabled.")
        else:
            self.io.tool_output("To change the setting, use '/context-management on' or 'off'.")

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

    def cmd_quit(self, args):
        "Exit the aider chat session"
        self.cmd_exit(args)

    async def run(self, inp):
        if not inp:
            return

        inp = inp.strip()
        if inp[0] in "/!":
            words = inp.split()
            cmd_name = words[0][1:]
            cmd_name = cmd_name.replace("-", "_")
            args = " ".join(words[1:])
        else:
            cmd_name = "help"
            args = ""

        cmd_method_name = f"cmd_{cmd_name}"
        cmd_method = getattr(self, cmd_method_name, None)

        if not cmd_method:
            self.io.tool_error(f"Error: Unknown command `{cmd_name}`")
            if self.help:
                self.help.show_help()
            return

        if asyncio.iscoroutinefunction(cmd_method):
            await cmd_method(args)
        else:
            cmd_method(args)

    def cmd_exit(self, args):
        "Exit the aider chat session"
        self.io.tool_output("Exiting...")
        raise SystemExit
