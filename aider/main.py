import asyncio
import glob
import json
import os
import re
import sys
import threading
import time
import traceback
import webbrowser
from dataclasses import fields
from pathlib import Path

try:
    import git
except ImportError:
    git = None

import importlib_resources
import shtab
from dotenv import load_dotenv
from prompt_toolkit.enums import EditingMode

from aider import __version__, models, urls, utils
from aider.analytics import Analytics
from aider.args import get_parser
from aider.coders import Coder
from aider.coders.base_coder import UnknownEditFormat
from aider.commands import Commands, SwitchCoder
from aider.copypaste import ClipboardWatcher
from aider.deprecated import handle_deprecated_model_args
from aider.format_settings import format_settings, scrub_sensitive_info
from aider.history import ChatSummary
from aider.io import InputOutput
from aider.llm import litellm  # noqa: F401; properly init litellm on launch
from aider.mcp import load_mcp_servers
from aider.models import ModelSettings
from aider.onboarding import offer_openrouter_oauth, select_default_model
from aider.repo import ANY_GIT_ERROR, GitRepo
from aider.report import report_uncaught_exceptions, set_args_error_data
from aider.sessions import SessionManager
from aider.versioncheck import check_version, install_from_main_branch, install_upgrade
from aider.watch import FileWatcher

from .dump import dump  # noqa: F401

PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
log_file = None
file_excludelist = ["get_bottom_toolbar", "<genexpr>"]


def custom_tracer(frame, event, arg):
    # Get the absolute path of the file where the code is executing
    filename = os.path.abspath(frame.f_code.co_filename)

    # --- THE FILTERING LOGIC ---
    # Only proceed if the file path is INSIDE the project root
    if not filename.startswith(PROJECT_ROOT):
        return None  # Returning None means no local trace function for this scope

    if filename.endswith("repo.py"):
        return None

    # If it's your code, trace the call
    if event == "call":
        func_name = frame.f_code.co_name
        line_no = frame.f_lineno

        if func_name not in file_excludelist:
            log_file.write(
                f"-> CALL: {func_name}() in {os.path.basename(filename)}:{line_no} -"
                f" {time.time()}\n"
            )

    if event == "return":
        func_name = frame.f_code.co_name
        line_no = frame.f_lineno

        if func_name not in file_excludelist:
            log_file.write(
                f"<- RETURN: {func_name}() in {os.path.basename(filename)}:{line_no} -"
                f" {time.time()}\n"
            )

    # Must return the trace function (or a local one) for subsequent events
    return custom_tracer


def check_config_files_for_yes(config_files):
    found = False
    for config_file in config_files:
        if Path(config_file).exists():
            try:
                with open(config_file, "r") as f:
                    for line in f:
                        if line.strip().startswith("yes:"):
                            print("Configuration error detected.")
                            print(f"The file {config_file} contains a line starting with 'yes:'")
                            print("Please replace 'yes:' with 'yes-always:' in this file.")
                            found = True
            except Exception:
                pass
    return found


def get_git_root():
    """Try and guess the git repo, since the conf.yml can be at the repo root"""
    try:
        repo = git.Repo(search_parent_directories=True)
        return repo.working_tree_dir
    except (git.InvalidGitRepositoryError, FileNotFoundError):
        return None


def guessed_wrong_repo(io, git_root, fnames, git_dname):
    """After we parse the args, we can determine the real repo. Did we guess wrong?"""

    try:
        check_repo = Path(GitRepo(io, fnames, git_dname).root).resolve()
    except (OSError,) + ANY_GIT_ERROR:
        return

    # we had no guess, rely on the "true" repo result
    if not git_root:
        return str(check_repo)

    git_root = Path(git_root).resolve()
    if check_repo == git_root:
        return

    return str(check_repo)


async def make_new_repo(git_root, io):
    try:
        repo = git.Repo.init(git_root)
        await check_gitignore(git_root, io, False)
    except ANY_GIT_ERROR as err:  # issue #1233
        io.tool_error(f"Unable to create git repo in {git_root}")
        io.tool_output(str(err))
        return

    io.tool_output(f"Git repository created in {git_root}")
    return repo


async def setup_git(git_root, io):
    if git is None:
        return

    try:
        cwd = Path.cwd()
    except OSError:
        cwd = None

    repo = None

    if git_root:
        try:
            repo = git.Repo(git_root)
        except ANY_GIT_ERROR:
            pass
    elif cwd == Path.home():
        io.tool_warning(
            "You should probably run aider in your project's directory, not your home dir."
        )
        return
    elif cwd and await io.confirm_ask(
        "No git repo found, create one to track aider's changes (recommended)?", acknowledge=True
    ):
        git_root = str(cwd.resolve())
        repo = await make_new_repo(git_root, io)

    if not repo:
        return

    try:
        user_name = repo.git.config("--get", "user.name") or None
    except git.exc.GitCommandError:
        user_name = None

    try:
        user_email = repo.git.config("--get", "user.email") or None
    except git.exc.GitCommandError:
        user_email = None

    if user_name and user_email:
        return repo.working_tree_dir

    with repo.config_writer() as git_config:
        if not user_name:
            git_config.set_value("user", "name", "Your Name")
            io.tool_warning('Update git name with: git config user.name "Your Name"')
        if not user_email:
            git_config.set_value("user", "email", "you@example.com")
            io.tool_warning('Update git email with: git config user.email "you@example.com"')

    return repo.working_tree_dir


async def check_gitignore(git_root, io, ask=True):
    if not git_root:
        return

    try:
        repo = git.Repo(git_root)
        patterns_to_add = []

        if not repo.ignored(".aider"):
            patterns_to_add.append(".aider*")

        env_path = Path(git_root) / ".env"
        if env_path.exists() and not repo.ignored(".env"):
            patterns_to_add.append(".env")

        if not patterns_to_add:
            return

        gitignore_file = Path(git_root) / ".gitignore"
        if gitignore_file.exists():
            try:
                content = io.read_text(gitignore_file)
                if content is None:
                    return
                if not content.endswith("\n"):
                    content += "\n"
            except OSError as e:
                io.tool_error(f"Error when trying to read {gitignore_file}: {e}")
                return
        else:
            content = ""
    except ANY_GIT_ERROR:
        return

    if ask:
        io.tool_output("You can skip this check with --no-gitignore")
        if not await io.confirm_ask(
            f"Add {', '.join(patterns_to_add)} to .gitignore (recommended)?",
            acknowledge=True,
        ):
            return

    content += "\n".join(patterns_to_add) + "\n"

    try:
        io.write_text(gitignore_file, content)
        io.tool_output(f"Added {', '.join(patterns_to_add)} to .gitignore")
    except OSError as e:
        io.tool_error(f"Error when trying to write to {gitignore_file}: {e}")
        io.tool_output(
            "Try running with appropriate permissions or manually add these patterns to .gitignore:"
        )
        for pattern in patterns_to_add:
            io.tool_output(f"  {pattern}")


async def check_streamlit_install(io):
    return await utils.check_pip_install_extra(
        io,
        "streamlit",
        "You need to install the aider browser feature",
        ["aider-ce[browser]"],
    )


async def write_streamlit_credentials():
    from streamlit.file_util import get_streamlit_file_path

    # See https://github.com/Aider-AI/aider/issues/772

    credential_path = Path(get_streamlit_file_path()) / "credentials.toml"
    if not os.path.exists(credential_path):
        empty_creds = '[general]\nemail = ""\n'

        os.makedirs(os.path.dirname(credential_path), exist_ok=True)
        with open(credential_path, "w") as f:
            f.write(empty_creds)
    else:
        print("Streamlit credentials already exist.")


async def launch_gui(args):
    from streamlit.web import cli

    from aider import gui

    print()
    print("CONTROL-C to exit...")

    # Necessary so streamlit does not prompt the user for an email address.
    await write_streamlit_credentials()

    target = gui.__file__

    st_args = ["run", target]

    st_args += [
        "--browser.gatherUsageStats=false",
        "--runner.magicEnabled=false",
        "--server.runOnSave=false",
    ]

    # https://github.com/Aider-AI/aider/issues/2193
    is_dev = "-dev" in str(__version__)

    if is_dev:
        print("Watching for file changes.")
    else:
        st_args += [
            "--global.developmentMode=false",
            "--server.fileWatcherType=none",
            "--client.toolbarMode=viewer",  # minimal?
        ]

    st_args += ["--"] + args

    cli.main(st_args)

    # from click.testing import CliRunner
    # runner = CliRunner()
    # from streamlit.web import bootstrap
    # bootstrap.load_config_options(flag_options={})
    # cli.main_run(target, args)
    # sys.argv = ['streamlit', 'run', '--'] + args


def parse_lint_cmds(lint_cmds, io):
    err = False
    res = dict()
    for lint_cmd in lint_cmds:
        if re.match(r"^[a-z]+:.*", lint_cmd):
            pieces = lint_cmd.split(":")
            lang = pieces[0]
            cmd = lint_cmd[len(lang) + 1 :]
            lang = lang.strip()
        else:
            lang = None
            cmd = lint_cmd

        cmd = cmd.strip()

        if cmd:
            res[lang] = cmd
        else:
            io.tool_error(f'Unable to parse --lint-cmd "{lint_cmd}"')
            io.tool_output('The arg should be "language: cmd --args ..."')
            io.tool_output('For example: --lint-cmd "python: flake8 --select=E9"')
            err = True
    if err:
        return
    return res


def generate_search_path_list(default_file, git_root, command_line_file):
    files = []
    files.append(Path.home() / default_file)  # homedir
    if git_root:
        files.append(Path(git_root) / default_file)  # git root
    files.append(default_file)
    if command_line_file:
        files.append(command_line_file)

    resolved_files = []
    for fn in files:
        try:
            resolved_files.append(Path(fn).expanduser().resolve())
        except OSError:
            pass

    files = resolved_files
    files.reverse()
    uniq = []
    for fn in files:
        if fn not in uniq:
            uniq.append(fn)
    uniq.reverse()
    files = uniq
    files = list(map(str, files))
    files = list(dict.fromkeys(files))

    return files


def register_models(git_root, model_settings_fname, io, verbose=False):
    model_settings_files = generate_search_path_list(
        ".aider.model.settings.yml", git_root, model_settings_fname
    )

    try:
        files_loaded = models.register_models(model_settings_files)
        if len(files_loaded) > 0:
            if verbose:
                io.tool_output("Loaded model settings from:")
                for file_loaded in files_loaded:
                    io.tool_output(f"  - {file_loaded}")  # noqa: E221
        elif verbose:
            io.tool_output("No model settings files loaded")
    except Exception as e:
        io.tool_error(f"Error loading aider model settings: {e}")
        return 1

    if verbose:
        io.tool_output("Searched for model settings files:")
        for file in model_settings_files:
            io.tool_output(f"  - {file}")

    return None


def load_dotenv_files(git_root, dotenv_fname, encoding="utf-8"):
    # Standard .env file search path
    dotenv_files = generate_search_path_list(
        ".env",
        git_root,
        dotenv_fname,
    )

    # Explicitly add the OAuth keys file to the beginning of the list
    oauth_keys_file = Path.home() / ".aider" / "oauth-keys.env"
    if oauth_keys_file.exists():
        # Insert at the beginning so it's loaded first (and potentially overridden)
        dotenv_files.insert(0, str(oauth_keys_file.resolve()))
        # Remove duplicates if it somehow got included by generate_search_path_list
        dotenv_files = list(dict.fromkeys(dotenv_files))

    loaded = []
    for fname in dotenv_files:
        try:
            if Path(fname).exists():
                load_dotenv(fname, override=True, encoding=encoding)
                loaded.append(fname)
        except OSError as e:
            print(f"OSError loading {fname}: {e}")
        except Exception as e:
            print(f"Error loading {fname}: {e}")
    return loaded


def register_litellm_models(git_root, model_metadata_fname, io, verbose=False):
    model_metadata_files = []

    # Add the resource file path
    resource_metadata = importlib_resources.files("aider.resources").joinpath("model-metadata.json")
    model_metadata_files.append(str(resource_metadata))

    model_metadata_files += generate_search_path_list(
        ".aider.model.metadata.json", git_root, model_metadata_fname
    )

    try:
        model_metadata_files_loaded = models.register_litellm_models(model_metadata_files)
        if len(model_metadata_files_loaded) > 0 and verbose:
            io.tool_output("Loaded model metadata from:")
            for model_metadata_file in model_metadata_files_loaded:
                io.tool_output(f"  - {model_metadata_file}")  # noqa: E221
    except Exception as e:
        io.tool_error(f"Error loading model metadata models: {e}")
        return 1


async def sanity_check_repo(repo, io):
    if not repo:
        return True

    if not repo.repo.working_tree_dir:
        io.tool_error("The git repo does not seem to have a working tree?")
        return False

    bad_ver = False
    try:
        repo.get_tracked_files()
        if not repo.git_repo_error:
            return True
        error_msg = str(repo.git_repo_error)
    except UnicodeDecodeError as exc:
        error_msg = (
            "Failed to read the Git repository. This issue is likely caused by a path encoded "
            f'in a format different from the expected encoding "{sys.getfilesystemencoding()}".\n'
            f"Internal error: {str(exc)}"
        )
    except ANY_GIT_ERROR as exc:
        error_msg = str(exc)
        bad_ver = "version in (1, 2)" in error_msg
    except AssertionError as exc:
        error_msg = str(exc)
        bad_ver = True

    if bad_ver:
        io.tool_error("Aider only works with git repos with version number 1 or 2.")
        io.tool_output("You may be able to convert your repo: git update-index --index-version=2")
        io.tool_output("Or run aider --no-git to proceed without using git.")
        await io.offer_url(
            urls.git_index_version, "Open documentation url for more info?", acknowledge=True
        )
        return False

    io.tool_error("Unable to read git repository, it may be corrupt?")
    io.tool_output(error_msg)
    return False


def expand_glob_patterns(patterns, root="."):
    """Expand glob patterns in a list of file paths."""
    expanded_files = []
    for pattern in patterns:
        # Check if the pattern contains glob characters
        if any(c in pattern for c in "*?[]"):
            # Use glob to expand the pattern
            matches = glob.glob(pattern, recursive=True)
            if matches:
                expanded_files.extend(matches)
            else:
                # If no matches, keep the original pattern
                expanded_files.append(pattern)
        else:
            # Not a glob pattern, keep as is
            expanded_files.append(pattern)
    return expanded_files


async def discover_and_load_tools(coder, git_root, args):
    from aider.coders.agent_coder import AgentCoder

    # Only proceed if the current coder is a AgentCoder
    if not isinstance(coder, AgentCoder):
        return

    # Only run tool discovery once per session
    if coder.did_discover_tools:
        return

    if not hasattr(coder, "tool_add_from_path"):
        return

    tool_dirs = []

    # 1. Global tools directory
    global_tools_dir = Path.home() / ".aider.tools"
    if global_tools_dir.is_dir():
        tool_dirs.append(global_tools_dir)

    # 2. Project-specific tools directory
    if git_root:
        project_tools_dir = Path(git_root) / ".aider.tools"
        if project_tools_dir.is_dir():
            tool_dirs.append(project_tools_dir)

    # 3. Directories from --tools-dir argument
    if args.tools_dir:
        for d in args.tools_dir:
            path = Path(d)
            if path.is_dir():
                tool_dirs.append(path)
            else:
                coder.io.tool_warning(f"Tools directory not found: {d}")

    if not tool_dirs:
        if coder.verbose:
            coder.io.tool_output("No tool directories configured or found.")
        coder.did_discover_tools = True  # Mark as discovered even if empty
        return

    discovered_tools = []
    discovered_paths = set()

    if coder.verbose:
        coder.io.tool_output("Scanning for custom tools...")

    for tool_dir in tool_dirs:
        if coder.verbose:
            coder.io.tool_output(f"  Scanning directory: {tool_dir}")
        try:
            for entry in os.scandir(tool_dir):
                if entry.is_file() and entry.name.endswith(".py"):
                    tool_file = Path(entry.path)
                    resolved_path = tool_file.resolve()
                    if resolved_path not in discovered_paths:
                        discovered_tools.append(tool_file)
                        discovered_paths.add(resolved_path)
                        if coder.verbose:
                            coder.io.tool_output(f"    Discovered tool: {tool_file}")
        except OSError as e:
            coder.io.tool_warning(f"  Warning: Could not scan directory {tool_dir}: {e}")

    if not discovered_tools:
        if coder.verbose:
            coder.io.tool_output("No custom tools found in scanned directories.")
        coder.did_discover_tools = True  # Mark as discovered even if empty
        return

    coder.io.tool_output("Discovered custom tools:")
    tool_paths_str = "\n".join([f"  - {path}" for path in discovered_tools])

    warning_message = (
        f"WARNING: This will execute the Python code in the following files:\n{tool_paths_str}\n"
        "Only load tools from sources you trust."
    )
    question = "Load these tools?"

    if await coder.io.confirm_ask(question, default="y", subject=warning_message):
        for tool_path in discovered_tools:
            try:
                coder.tool_add_from_path(str(tool_path))
            except Exception as e:
                coder.io.tool_error(f"Failed to load tool {tool_path}: {e}")

    coder.did_discover_tools = True  # Mark as discovered after processing


def main(argv=None, input=None, output=None, force_git_root=None, return_coder=False):
    return asyncio.run(main_async(argv, input, output, force_git_root, return_coder))


async def main_async(argv=None, input=None, output=None, force_git_root=None, return_coder=False):
    report_uncaught_exceptions()

    if argv is None:
        argv = sys.argv[1:]

    if git is None:
        git_root = None
    elif force_git_root:
        git_root = force_git_root
    else:
        git_root = get_git_root()

    conf_fname = Path(".aider.conf.yml")

    default_config_files = []
    try:
        default_config_files += [conf_fname.resolve()]  # CWD
    except OSError:
        pass

    if git_root:
        git_conf = Path(git_root) / conf_fname  # git root
        if git_conf not in default_config_files:
            default_config_files.append(git_conf)
    default_config_files.append(Path.home() / conf_fname)  # homedir
    default_config_files = list(map(str, default_config_files))

    parser = get_parser(default_config_files, git_root)
    try:
        args, unknown = parser.parse_known_args(argv)
    except AttributeError as e:
        if all(word in str(e) for word in ["bool", "object", "has", "no", "attribute", "strip"]):
            if check_config_files_for_yes(default_config_files):
                return 1
        raise e

    if args.verbose:
        print("Config files search order, if no --config:")
        for file in default_config_files:
            exists = "(exists)" if Path(file).exists() else ""
            print(f"  - {file} {exists}")

    default_config_files.reverse()

    parser = get_parser(default_config_files, git_root)

    args, unknown = parser.parse_known_args(argv)

    # Load the .env file specified in the arguments
    loaded_dotenvs = load_dotenv_files(git_root, args.env_file, args.encoding)

    # Parse again to include any arguments that might have been defined in .env
    args = parser.parse_args(argv)
    set_args_error_data(args)

    if args.debug:
        global log_file
        os.makedirs(".aider/logs/", exist_ok=True)
        log_file = open(".aider/logs/debug.log", "w", buffering=1)
        sys.settrace(custom_tracer)

    if args.shell_completions:
        # Ensure parser.prog is set for shtab, though it should be by default
        parser.prog = "aider"
        print(shtab.complete(parser, shell=args.shell_completions))
        sys.exit(0)

    if git is None:
        args.git = False

    if args.analytics_disable:
        analytics = Analytics(permanently_disable=True)
        print("Analytics have been permanently disabled.")

    if not args.verify_ssl:
        import httpx

        os.environ["SSL_VERIFY"] = ""
        litellm._load_litellm()
        litellm._lazy_module.client_session = httpx.Client(verify=False)
        litellm._lazy_module.aclient_session = httpx.AsyncClient(verify=False)
        # Set verify_ssl on the model_info_manager
        models.model_info_manager.set_verify_ssl(False)

    models.set_litellm_reinit_timestamp()

    if args.timeout:
        models.request_timeout = args.timeout

    if args.dark_mode:
        args.user_input_color = "#32FF32"
        args.tool_error_color = "#FF3333"
        args.tool_warning_color = "#FFFF00"
        args.assistant_output_color = "#00FFFF"
        args.code_theme = "monokai"

    if args.light_mode:
        args.user_input_color = "blue"
        args.tool_error_color = "red"
        args.tool_warning_color = "#FFA500"
        args.assistant_output_color = "blue"
        args.code_theme = "default"

    if args.tool_output_color:
        args.tool_output_color = args.tool_output_color.lower()

    if args.speech_output_language:
        args.speak = True

    io = InputOutput(
        pretty=args.pretty,
        yes=args.yes,
        input_history_file=args.input_history_file,
        chat_history_file=args.chat_history_file,
        input=input,
        output=output,
        user_input_color=args.user_input_color,
        tool_output_color=args.tool_output_color,
        tool_error_color=args.tool_error_color,
        tool_warning_color=args.tool_warning_color,
        assistant_output_color=args.assistant_output_color,
        completion_menu_color=args.completion_menu_color,
        completion_menu_bg_color=args.completion_menu_bg_color,
        completion_menu_current_color=args.completion_menu_current_color,
        completion_menu_current_bg_color=args.completion_menu_current_bg_color,
        code_theme=args.code_theme,
        encoding=args.encoding,
        line_endings=args.line_endings,
        dry_run=args.dry_run,
        llm_history_file=args.llm_history_file,
        editingmode=EditingMode.VI if args.vim_bindings else EditingMode.EMACS,
        fancy_input=not args.no_fancy_input,
        multiline_mode=args.multiline_input,
        notifications=args.notifications,
        notifications_command=args.notifications_command,
        verbose=args.verbose,
    )

    if args.verbose:
        io.tool_output("Loaded .env files:")
        for fname in loaded_dotenvs:
            io.tool_output(f"  - {fname}")

    if args.gui:
        await launch_gui(argv)
        return

    if args.check_update:
        await check_version(io, just_check=True)
        return
    if args.update:
        await install_upgrade(io)
        return
    if args.install_main:
        await install_from_main_branch(io)
        return

    if args.help_extra:
        install_help_extra(io)
        return

    if args.version:
        print(__version__)
        return

    if args.models:
        models.print_matching_models(io, args.models)
        return

    if args.tools_help:
        from aider.coders.agent_coder import AgentCoder

        AgentCoder.show_tool_help(io)
        return

    if args.lint_cmds:
        args.lint_cmds = parse_lint_cmds(args.lint_cmds, io)
        if args.lint_cmds is None:
            return 1

    if args.format_cmds:
        args.format_cmds = parse_lint_cmds(args.format_cmds, io)
        if args.format_cmds is None:
            return 1

    if args.files:
        args.files = expand_glob_patterns(args.files)

    if args.read_only_files:
        args.read_only_files = expand_glob_patterns(args.read_only_files)

    if args.read_only_stubs_files:
        args.read_only_stubs_files = expand_glob_patterns(args.read_only_stubs_files)

    if args.read_command and not os.path.exists(args.read_command):
        io.tool_error(f"File not found: {args.read_command}")
        return 1

    if args.edit_command and not os.path.exists(args.edit_command):
        io.tool_error(f"File not found: {args.edit_command}")
        return 1

    if args.edit_format == "diff" and not args.git:
        io.tool_error("The `diff` edit format requires a git repository.")
        io.tool_error("Please run aider in a git repository or use a different edit format.")
        return 1

    if args.yes is None:
        if args.yes_always:
            args.yes = True
        elif args.yes_never:
            args.yes = False

    if args.yes is True and args.yes is False:
        io.tool_error("Cannot use both --yes-always and --yes-never")
        return 1

    if args.yes is True and args.show_diff:
        io.tool_error("Cannot use both --yes-always and --show-diff")
        return 1

    if args.yes is True and args.prompt_commit:
        io.tool_error("Cannot use both --yes-always and --prompt-commit")
        return 1

    if args.auto_commits and args.dirty_commits:
        io.tool_error("Cannot use both --auto-commits and --dirty-commits")
        return 1

    if args.auto_commits is False and args.prompt_commit:
        io.tool_error("Cannot use both --no-auto-commits and --prompt-commit")
        return 1

    if args.dirty_commits and args.prompt_commit:
        io.tool_error("Cannot use both --dirty-commits and --prompt-commit")
        return 1

    if args.auto_commits is False and args.show_diff:
        io.tool_error("Cannot use both --no-auto-commits and --show-diff")
        return 1

    if args.dirty_commits and args.show_diff:
        io.tool_error("Cannot use both --dirty-commits and --show-diff")
        return 1

    if args.auto_commits is False and args.dry_run:
        io.tool_error("Cannot use both --no-auto-commits and --dry-run")
        return 1

    if args.dirty_commits and args.dry_run:
        io.tool_error("Cannot use both --dirty-commits and --dry-run")
        return 1

    if args.auto_commits is False and args.yes is True:
        io.tool_error("Cannot use both --no-auto-commits and --yes-always")
        return 1

    if args.dirty_commits and args.yes is True:
        io.tool_error("Cannot use both --dirty-commits and --yes-always")
        return 1

    if args.auto_commits is False and args.voice_language:
        io.tool_error("Cannot use both --no-auto-commits and --voice-language")
        return 1

    if args.dirty_commits and args.voice_language:
        io.tool_error("Cannot use both --dirty-commits and --voice-language")
        return 1

    if args.auto_commits is False and args.show_repo_map:
        io.tool_error("Cannot use both --no-auto-commits and --show-repo-map")
        return 1

    if args.dirty_commits and args.show_repo_map:
        io.tool_error("Cannot use both --dirty-commits and --show-repo-map")
        return 1

    if args.auto_commits is False and args.map_tokens:
        io.tool_error("Cannot use both --no-auto-commits and --map-tokens")
        return 1

    if args.dirty_commits and args.map_tokens:
        io.tool_error("Cannot use both --dirty-commits and --map-tokens")
        return 1

    if args.auto_commits is False and args.map_mul_no_files:
        io.tool_error("Cannot use both --no-auto-commits and --map-mul-no-files")
        return 1

    if args.dirty_commits and args.map_mul_no_files:
        io.tool_error("Cannot use both --dirty-commits and --map-mul-no-files")
        return 1

    if args.auto_commits is False and args.map_style:
        io.tool_error("Cannot use both --no-auto-commits and --map-style")
        return 1

    if args.dirty_commits and args.map_style:
        io.tool_error("Cannot use both --dirty-commits and --map-style")
        return 1

    if args.auto_commits is False and args.tags_in_code:
        io.tool_error("Cannot use both --no-auto-commits and --tags-in-code")
        return 1

    if args.dirty_commits and args.tags_in_code:
        io.tool_error("Cannot use both --dirty-commits and --tags-in-code")
        return 1

    if args.auto_commits is False and args.ctags_cmd:
        io.tool_error("Cannot use both --no-auto-commits and --ctags-cmd")
        return 1

    if args.dirty_commits and args.ctags_cmd:
        io.tool_error("Cannot use both --dirty-commits and --ctags-cmd")
        return 1

    if args.auto_commits is False and args.ctags_update_on_commit:
        io.tool_error("Cannot use both --no-auto-commits and --ctags-update-on-commit")
        return 1

    if args.dirty_commits and args.ctags_update_on_commit:
        io.tool_error("Cannot use both --dirty-commits and --ctags-update-on-commit")
        return 1

    if args.auto_commits is False and args.ctags_auto_update:
        io.tool_error("Cannot use both --no-auto-commits and --ctags-auto-update")
        return 1

    if args.dirty_commits and args.ctags_auto_update:
        io.tool_error("Cannot use both --dirty-commits and --ctags-auto-update")
        return 1

    if args.auto_commits is False and args.ctags_commit_message:
        io.tool_error("Cannot use both --no-auto-commits and --ctags-commit-message")
        return 1

    if args.dirty_commits and args.ctags_commit_message:
        io.tool_error("Cannot use both --dirty-commits and --ctags-commit-message")
        return 1

    if args.auto_commits is False and args.ctags_message:
        io.tool_error("Cannot use both --no-auto-commits and --ctags-message")
        return 1

    if args.dirty_commits and args.ctags_message:
        io.tool_error("Cannot use both --dirty-commits and --ctags-message")
        return 1

    if args.auto_commits is False and args.ctags_quiet:
        io.tool_error("Cannot use both --no-auto-commits and --ctags-quiet")
        return 1

    if args.dirty_commits and args.ctags_quiet:
        io.tool_error("Cannot use both --dirty-commits and --ctags-quiet")
        return 1

    if args.auto_commits is False and args.ctags_read_only:
        io.tool_error("Cannot use both --no-auto-commits and --ctags-read-only")
        return 1

    if args.dirty_commits and args.ctags_read_only:
        io.tool_error("Cannot use both --dirty-commits and --ctags-read-only")
        return 1

    if args.auto_commits is False and args.ctags_update_all:
        io.tool_error("Cannot use both --no-auto-commits and --ctags-update-all")
        return 1

    if args.dirty_commits and args.ctags_update_all:
        io.tool_error("Cannot use both --dirty-commits and --ctags-update-all")
        return 1

    if args.auto_commits is False and args.ctags_update_untracked:
        io.tool_error("Cannot use both --no-auto-commits and --ctags-update-untracked")
        return 1

    if args.dirty_commits and args.ctags_update_untracked:
        io.tool_error("Cannot use both --dirty-commits and --ctags-update-untracked")
        return 1

    if args.auto_commits is False and args.ctags_update_unignored:
        io.tool_error("Cannot use both --no-auto-commits and --ctags-update-unignored")
        return 1

    if args.dirty_commits and args.ctags_update_unignored:
        io.tool_error("Cannot use both --dirty-commits and --ctags-update-unignored")
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked:
        io.tool_error("Cannot use both --no-auto-commits and --ctags-update-all-with-untracked")
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked:
        io.tool_error("Cannot use both --dirty-commits and --ctags-update-all-with-untracked")
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_unignored:
        io.tool_error("Cannot use both --no-auto-commits and --ctags-update-all-with-unignored")
        return 1

    if args.dirty_commits and args.ctags_update_all_with_unignored:
        io.tool_error("Cannot use both --dirty-commits and --ctags-update-all-with-unignored")
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --no-auto-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.dirty_commits and args.ctags_update_all_with_untracked_and_unignored:
        io.tool_error(
            "Cannot use both --dirty-commits and --ctags-update-all-with-untracked-and-unignored"
        )
        return 1

    if args.auto_commits is False and args.ctags_Model API Response Error. Please retry the previous request