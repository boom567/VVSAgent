# VVSAgent (Python)

A modular local AI agent with pluggable skills, multiple model providers, and a bridge mode for editor integration.

This repository now includes a CLI-Anything bridge skill, so the agent can use many `cli-anything-*` tools through unified skills.

## What Is Included

- Core agent runtime: `aiagent.py`
- Coding-focused entrypoint: `coding_agent.py`
- Bridge protocol server: `agent_server_bridge.py`
- Built-in local skills under `skills/`
- Embedded CLI-Anything ecosystem under `CLI-Anything/`
- VS Code extension under `vscode-agent-chat/`

## Quick Start

### 1) Activate environment

```bash
conda activate agent
```

### 2) Install Python dependencies

```bash
python -m pip install -r requirements.txt
```

### 3) Run the agent

```bash
python aiagent.py
```

Or use scripts:

```bash
./agentStart
```

### 4) Run coding agent

```bash
python coding_agent.py
# or
./codingAgentStart
```

## CLI-Anything Bridge (New)

The agent auto-loads `skills/cli_anything_bridge_skill.py` and exposes:

- `cli_anything_list`
- `cli_anything_info`
- `cli_anything_install`
- `cli_anything_run`
- `cli_anything_local_catalog`
- `cli_anything_templates`

It also auto-registers local proxy skills like:

- `ca_blender`
- `ca_gimp`
- `ca_browser`
- ... (many `ca_*` tools from local `CLI-Anything` harnesses)

### Runtime behavior

- Prefer system commands if installed (`cli-hub`, `cli-anything-*`)
- Fallback to local source harness execution from `CLI-Anything/<tool>/agent-harness`
- Safe subprocess execution (no shell string concatenation)
- JSON-wrapped outputs for stable agent parsing

## Blender Setup

If you want real rendering, Blender executable must be available in `PATH`.

### Install Blender on macOS

```bash
brew install --cask blender
```

### Ensure `blender` command is available

Check first:

```bash
command -v blender
blender --version
```

If not found, choose one method.

#### Method A: Add app binary directory to PATH

```bash
echo 'export PATH="/Applications/Blender.app/Contents/MacOS:$PATH"' >> ~/.zshrc
source ~/.zshrc
command -v blender
blender --version
```

#### Method B: Create symlink

Apple Silicon:

```bash
sudo ln -sf /Applications/Blender.app/Contents/MacOS/Blender /opt/homebrew/bin/blender
```

Intel macOS:

```bash
sudo ln -sf /Applications/Blender.app/Contents/MacOS/Blender /usr/local/bin/blender
```

Then verify:

```bash
command -v blender
blender --version
```

## Blender Usage Through Agent Skills

### Discover templates

Use `cli_anything_templates` with `tool_name=blender` to get recommended command patterns extracted from SKILL docs.

### Minimal smoke test

`ca_blender` can be used immediately for scene JSON operations:

```text
command_args = scene new -o /tmp/blender_check.blend-cli.json
json_mode = true
```

### Example workflow

```text
ca_blender(command_args="scene new -o myscene.blend-cli.json")
ca_blender(command_args="object add cube --name Box --location 0,0,1")
ca_blender(command_args="render execute output.png --overwrite")
```

## Notes

- Scene editing and JSON operations can work without launching Blender UI.
- Real render/export operations require `blender` executable to be available.
- Some unrelated optional skills may fail to load if dependencies are missing (for example `openpyxl`, `sounddevice`).

## Dependency Troubleshooting

- If `sounddevice` install fails on macOS, ensure PortAudio is installed first:

```bash
brew install portaudio
python -m pip install sounddevice
```

- If mouse/keyboard automation is blocked, grant Accessibility permission to your terminal (or Python process) in macOS System Settings.

## Key Files

- `aiagent.py`
- `agent_server_bridge.py`
- `skills/cli_anything_bridge_skill.py`
- `CLI-Anything/blender/agent-harness/cli_anything/blender/utils/blender_backend.py`
