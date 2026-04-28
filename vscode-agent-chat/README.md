# Local Agent Chat VS Code Extension

This extension lets you chat with your local Python agent from inside VS Code.

## Features

- Open a chat panel from command palette.
- Send messages to your local agent in a continuous session.
- Reset conversation state.

## Requirements

- A Python environment that can run your project agent.
- The bridge script file at workspace root: `agent_server_bridge.py`.

## Extension Settings

- `vvsagant.pythonPath`: Python executable path.
- `vvsagant.bridgeScript`: Bridge script path relative to workspace root.

## Development

1. Open folder `vscode-agent-chat` in VS Code.
2. Run `npm install`.
3. Run `npm run compile`.
4. Press `F5` to start an Extension Development Host.
5. In command palette run `VVSAgant: Open Chat`.

## Notes

The extension starts a long-running Python bridge process and communicates with JSON lines over stdin/stdout.
