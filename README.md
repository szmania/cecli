## Why `aider-ce`?

`aider-ce` (aka `cecli`, pronounced like "Cecily") is a community-driven fork of the [Aider](https://aider.chat/) AI pair programming tool.
Aider is a fantastic piece of software with a wonderful community but it has been painfully slow in receiving updates in the quickly evolving AI tooling space.

We aim to foster an open, collaborative ecosystem where new features, experiments, and improvements can be developed and shared rapidly. We believe in genuine FOSS principles and actively welcome contributors of all skill levels.

If you are looking for bleeding-edge features or want to get your hands dirty with the internals of an AI coding agent, here's your sign.
LLMs are a part of our lives from here on out so join us in learning about and crafting the future.

### Links

[Discord Chat](https://discord.gg/AX9ZEA7nJn) 🞄
[Changelog](https://github.com/dwash96/aider-ce/blob/main/CHANGELOG.md) 🞄
[Issue Queue](https://github.com/dwash96/aider-ce/issues)

## Documentation/Other Notes:

* [Agent Mode](https://github.com/dwash96/aider-ce/blob/main/aider/website/docs/config/agent-mode.md)
* [MCP Configuration](https://github.com/dwash96/aider-ce/blob/main/aider/website/docs/config/mcp.md)
* [Session Management](https://github.com/dwash96/aider-ce/blob/main/aider/website/docs/sessions.md)
* [Skills](https://github.com/dwash96/aider-ce/blob/main/aider/website/docs/config/skills.md)
* [Aider Original Documentation (still mostly applies)](https://aider.chat/)

You can see a selection of the enhancements and updates by comparing the help output:
```bash
aider --help > aider.help.txt
cecli --help > cecli.help.txt
diff aider.help.txt cecli.help.txt -uw --color
```

## Installation Instructions
This project can be installed using several methods:

### Package Installation
```bash
pip install aider-ce
```

or

```bash
uv pip install aider-ce
```

The package exports an `aider-ce` command that accepts all of Aider's configuration options

### Tool Installation
```bash
uv tool install --python python3.12 aider-ce
```

Use the tool installation so aider doesn't interfere with your development environment

## Configuration

The documentation above contains the full set of allowed configuration options
but I highly recommend using an `.aider.conf.yml` file. A good place to get started is:

```yaml
model: <model of your choice>
agent: true
analytics: false
auto-commits: true
auto-save: true
auto-load: false
cache-prompts: true
check-update: true
debug: false
enable-context-compaction: true
env-file: .aider.env
multiline: true
preserve-todo-list: true
show-model-warnings: true
watch-files: false
agent-config: |
  {
    "large_file_token_threshold": 12500,
    "skip_cli_confirmations": false
  }
mcp-servers: |
  {
    "mcpServers":
      {
        "context7":{
          "transport":"http",
          "url":"https://mcp.context7.com/mcp"
        }
      }
  }
```

Use the adjacent .aider.env file to store model api keys as environment variables, e.g:

```
ANTHROPIC_API_KEY="..."
GEMINI_API_KEY="..."
OPENAI_API_KEY="..."
OPENROUTER_API_KEY="..."
DEEPSEEK_API_KEY="..."
```

### Run Program

If you are in the directory with your .aider.conf.yml file, then simply running `aider-ce` or `cecli` will start the agent with your configuration. If you want additional sandboxing, we publish a docker container that can be ran as follows:

```bash
docker pull dustinwashington/aider-ce
docker run \
  -it \
  --user $(id -u):$(id -g) \
  --volume $(pwd):/app dustinwashington/aider-ce \
  --volume $(pwd)/.aider.conf.yml:/.aider.conf.yml \
  --volume $(pwd)/.aider.env:/.aider/.env \
  --config /app/.aider.conf.yml
```

This command will make sure all commands ran by the coding agent happen in context of the docker container to protect the host file system from any infamous agentic mishap

## Project Roadmap/Goals

The current priorities are to improve core capabilities and user experience of the Aider project

1. **Base Asynchronicity (aider-ce coroutine-experiment branch)**
  * [x] Refactor codebase to have the main loop run asynchronously
  * [x] Update test harness to work with new asynchronous methods

2. **Repo Map Accuracy** - [Discussion](https://github.com/dwash96/aider-ce/issues/45)
  * [x] [Bias page ranking toward active/editable files in repo map parsing](https://github.com/Aider-AI/aider/issues/2405)
  * [x] [Include import information in repo map for richer context](https://github.com/Aider-AI/aider/issues/2688)
  * [x] [Handle non-unique symbols that break down in large codebases](https://github.com/Aider-AI/aider/issues/2341)

3. **Context Discovery** - [Discussion](https://github.com/dwash96/aider-ce/issues/46)
  * [ ] Develop AST-based search capabilities
  * [ ] Enhance file search with ripgrep integration
  * [ ] Implement RAG (Retrieval-Augmented Generation) for better code retrieval
  * [ ] Build an explicit workflow and local tooling for internal discovery mechanisms

4. **Context Delivery** - [Discussion](https://github.com/dwash96/aider-ce/issues/47)
  * [ ] Use workflow for internal discovery to better target file snippets needed for specific tasks
  * [ ] Add support for partial files and code snippets in model completion messages

5. **TUI Experience** - [Discussion](https://github.com/dwash96/aider-ce/issues/48)
  * [ ] Add a full TUI (probably using textual) to have a visual interface competitive with the other coding agent terminal programs
  * [x] Re-integrate pretty output formatting
  * [ ] Implement a response area, a prompt area with current auto completion capabilities, and a helper area for management utility commands

6. **Agent Mode** - [Discussion](https://github.com/dwash96/aider-ce/issues/111)
  * [x] Renaming "navigator mode" to "agent mode" for simplicity
  * [x] Add an explicit "finished" internal tool
  * [x] Add a configuration json setting for agent mode to specify allowed local tools to use, tool call limits, etc.
  * [ ] Add a RAG tool for the model to ask questions about the codebase
  * [ ] Make the system prompts more aggressive about removing unneeded files/content from the context
  * [ ] Add a plugin-like system for allowing agent mode to use user-defined tools in simple python files
  * [x] Add a dynamic tool discovery tool to allow the system to have only the tools it needs in context

### All Contributors (Both Aider Main and Aider-CE)

<a href="https://github.com/paul-gauthier">@paul-gauthier</a>
<a href="https://github.com/dwash96">@dwash96</a>
<a href="https://github.com/tekacs">@tekacs</a>
<a href="https://github.com/ei-grad">@ei-grad</a>
<a href="https://github.com/joshuavial">@joshuavial</a>
<a href="https://github.com/chr15m">@chr15m</a>
<a href="https://github.com/fry69">@fry69</a>
<a href="https://github.com/quinlanjager">@quinlanjager</a>
<a href="https://github.com/caseymcc">@caseymcc</a>
<a href="https://github.com/shladnik">@shladnik</a>
<a href="https://github.com/itlackey">@itlackey</a>
<a href="https://github.com/tomjuggler">@tomjuggler</a>
<a href="https://github.com/szmania">@szmania</a>
<a href="https://github.com/vk4s">@vk4s</a>
<a href="https://github.com/titusz">@titusz</a>
<a href="https://github.com/daniel-vainsencher">@daniel-vainsencher</a>
<a href="https://github.com/bphd">@bphd</a>
<a href="https://github.com/akaihola">@akaihola</a>
<a href="https://github.com/jalammar">@jalammar</a>
<a href="https://github.com/schpet">@schpet</a>
<a href="https://github.com/iamFIREcracker">@iamFIREcracker</a>
<a href="https://github.com/KennyDizi">@KennyDizi</a>
<a href="https://github.com/ivanfioravanti">@ivanfioravanti</a>
<a href="https://github.com/mdeweerd">@mdeweerd</a>
<a href="https://github.com/fahmad91">@fahmad91</a>
<a href="https://github.com/itsmeknt">@itsmeknt</a>
<a href="https://github.com/cheahjs">@cheahjs</a>
<a href="https://github.com/youknow04">@youknow04</a>
<a href="https://github.com/pjcreath">@pjcreath</a>
<a href="https://github.com/pcamp">@pcamp</a>
<a href="https://github.com/ErichBSchulz">@ErichBSchulz</a>