# WaddleAI for VS Code Copilot Chat

Integrate WaddleAI's enterprise AI proxy with VS Code Copilot Chat for enhanced AI-powered development with multi-LLM support, conversation memory, and security features.

## Features

🚀 **Multi-LLM Support**: Access OpenAI GPT, Anthropic Claude, and Ollama models through a single interface
🧠 **Conversation Memory**: Context-aware responses that remember your development patterns
🔒 **Enterprise Security**: Built-in prompt injection protection and security scanning
📊 **Token Management**: Track usage across different models with normalized billing
⚡ **Intelligent Routing**: Automatic model selection based on cost, latency, and availability
🔧 **Custom Configuration**: Flexible endpoint configuration for on-premises deployments

## Installation

1. **Install from VS Code Marketplace**: Search for "WaddleAI" in the Extensions panel
2. **Get a WaddleAI API Key**: Visit your WaddleAI management portal to generate an API key
3. **Configure the Extension**: Use `Ctrl+Shift+P` → "WaddleAI: Set API Key"

## Quick Start

### 1. Set Your API Key
```
Ctrl+Shift+P → WaddleAI: Set API Key
```
Enter your API key (format: `wa-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`)

### 2. Select a Model
```
Ctrl+Shift+P → WaddleAI: Select Model
```
Choose from available models like GPT-4, Claude-3, or Llama2

### 3. Use in Copilot Chat
1. Open Copilot Chat (`Ctrl+Shift+I`)
2. Click the model selector dropdown
3. Select "WaddleAI" as your provider
4. Start chatting with AI assistance!

## Configuration

### Settings
Configure WaddleAI through VS Code settings (`File → Preferences → Settings` → Search "WaddleAI"):

```json
{
  "waddleai.apiEndpoint": "http://localhost:8000",
  "waddleai.defaultModel": "gpt-4",
  "waddleai.enableMemory": true,
  "waddleai.enableSecurityScanning": true,
  "waddleai.maxTokens": 2048,
  "waddleai.temperature": 0.7
}
```

### Available Models
- **GPT Models**: `gpt-4`, `gpt-3.5-turbo`
- **Claude Models**: `claude-3-opus-20240229`, `claude-3-sonnet-20240229`
- **Ollama Models**: `llama2`, `codellama` (requires Ollama server)

### Memory Enhancement
Enable conversation memory to get context-aware responses that remember:
- Your coding preferences and patterns
- Project-specific information
- Previous conversations and solutions

### Security Features
- **Prompt Injection Detection**: Automatically scans for malicious prompts
- **Jailbreak Prevention**: Blocks attempts to bypass AI safety guidelines
- **Data Protection**: Prevents accidental exposure of sensitive information

## Commands

| Command | Description |
|---------|-------------|
| `WaddleAI: Set API Key` | Securely store your WaddleAI API key |
| `WaddleAI: Select Model` | Choose which AI model to use |
| `WaddleAI: Test Connection` | Verify connection to WaddleAI server |
| `WaddleAI: Show Token Usage` | View your current token usage and quotas |
| `WaddleAI: Clear Memory` | Reset conversation memory |

## Advanced Usage

### Custom Endpoints
For enterprise deployments, configure your own WaddleAI endpoint:

```json
{
  "waddleai.apiEndpoint": "https://waddleai.yourcompany.com"
}
```

### Code Context Integration
WaddleAI automatically includes:
- Currently selected code in your questions
- Active workspace information
- File language context for better responses

### Usage Monitoring
Track your AI usage with built-in analytics:
- Token consumption per model
- Daily and monthly quota tracking
- Request history and patterns

## Troubleshooting

### Common Issues

**❌ "Authentication failed"**
- Verify your API key is correct
- Check that your WaddleAI account is active
- Ensure proper permissions are granted

**❌ "Connection failed"**
- Verify the WaddleAI server is running
- Check your endpoint configuration
- Ensure firewall/proxy settings allow access

**❌ "Model not available"**
- Check which models are enabled in your WaddleAI configuration
- Verify model permissions for your API key
- Try selecting a different model

### Debug Mode
Enable debug logging:
```json
{
  "waddleai.debug": true
}
```

View logs in: `View → Output → WaddleAI`

## Requirements

- **VS Code**: Version 1.85.0 or higher
- **WaddleAI Server**: Running proxy server with API access
- **API Key**: Valid WaddleAI API key with appropriate permissions

## Privacy & Security

- API keys are stored securely in VS Code's secret storage
- Communication with WaddleAI uses HTTPS encryption
- No code or conversations are stored locally by default
- Memory features can be disabled for maximum privacy

## Support

- **Documentation**: [WaddleAI Docs](https://docs.waddleai.com)
- **Issues**: [GitHub Issues](https://github.com/penguintechinc/waddleai/issues)
- **Community**: [Discord Server](https://discord.gg/waddleai)

## License

MIT License - see [LICENSE](LICENSE) for details.

---

**Developed by WaddleAI** | [Website](https://waddleai.com) | [Enterprise](https://waddleai.com/enterprise)
