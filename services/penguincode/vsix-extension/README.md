# Ollama Assistant

AI-powered code completion and chat for VS Code using local Ollama models.

## Features

- **Chat Sidebar** - Dedicated chat interface with streaming responses
- **Inline Code Completion** - AI suggestions as you type
- **Code Generation** - Generate code from comments or descriptions
- **Code Explanation** - Get detailed explanations of selected code
- **Code Refactoring** - Improve code quality with AI assistance
- **Code Fixing** - Fix bugs with context-aware suggestions
- **Multi-Model Support** - Use any Ollama model (llama, codellama, mistral, etc.)

## Requirements

- [Ollama](https://ollama.ai) installed and running locally
- At least one model pulled (e.g., `ollama pull codellama`)

## Installation

### Quick Install

```bash
./scripts/install.sh --install
```

### Manual Install

1. Install dependencies:
   ```bash
   npm install
   ```

2. Build the extension:
   ```bash
   npm run compile
   ```

3. Package the extension:
   ```bash
   npx vsce package
   ```

4. Install in VS Code:
   - Open VS Code
   - Go to Extensions (Ctrl+Shift+X)
   - Click "..." menu > "Install from VSIX..."
   - Select `ollama-assistant.vsix`

### Development

```bash
# Install dependencies
npm install

# Start watch mode
npm run watch

# Press F5 in VS Code to launch Extension Development Host
```

## Usage

### Chat

1. Click the Ollama icon in the activity bar
2. Type your message and press Enter
3. Responses stream in real-time

### Inline Completion

- Start typing code and wait for suggestions
- Press Tab to accept a suggestion
- Toggle with command: "Ollama: Toggle Inline Completion"

### Commands

| Command | Keybinding | Description |
|---------|------------|-------------|
| Generate Code | Ctrl+Shift+G | Generate code from comment |
| Explain Code | Ctrl+Shift+E | Explain selected code |
| Refactor Code | - | Refactor selected code |
| Fix Code | - | Fix issues in selected code |
| Select Model | - | Choose Ollama model |

Right-click in the editor to access commands from the context menu.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `ollamaAssistant.apiUrl` | `http://localhost:11434` | Ollama API URL |
| `ollamaAssistant.defaultModel` | `codellama` | Model for code completion |
| `ollamaAssistant.chatModel` | `llama3.1` | Model for chat |
| `ollamaAssistant.temperature` | `0.7` | Generation temperature |
| `ollamaAssistant.maxTokens` | `2048` | Max tokens per response |
| `ollamaAssistant.enableInlineCompletion` | `true` | Enable inline suggestions |
| `ollamaAssistant.inlineCompletionDebounceMs` | `500` | Debounce delay (ms) |
| `ollamaAssistant.contextLines` | `50` | Context lines for completion |

## Supported Languages

TypeScript, JavaScript, Python, Go, Rust, Java, C#, C, C++, PHP, Ruby, Swift, Kotlin

## Recommended Models

- **codellama** - Best for code completion and generation
- **llama3.1** - Great for chat and explanations
- **mistral** - Fast and capable general purpose
- **deepseek-coder** - Excellent for code tasks

Pull models with:
```bash
ollama pull codellama
ollama pull llama3.1
```

## Troubleshooting

### Ollama not detected

1. Ensure Ollama is installed: https://ollama.ai
2. Start Ollama: `ollama serve`
3. Check it's running: `curl http://localhost:11434/api/tags`

### No models available

Pull a model:
```bash
ollama pull codellama
```

### Slow responses

- Use a smaller model (7B vs 13B)
- Reduce `maxTokens` in settings
- Ensure sufficient RAM/VRAM

## License

AGPL-3.0 - See LICENSE file

## Contributing

Contributions welcome! Please read the contributing guidelines first.

---

Made with love by Penguin Tech Inc.
