# WaddleAI VS Code Extension - Installation Guide

This guide will walk you through installing and configuring the WaddleAI extension for VS Code Copilot Chat.

## Prerequisites

1. **VS Code**: Version 1.85.0 or higher
2. **WaddleAI Server**: Running instance of WaddleAI proxy server
3. **API Key**: Valid WaddleAI API key with appropriate permissions

## Installation Methods

### Method 1: From VS Code Marketplace (Recommended)

1. Open VS Code
2. Go to Extensions panel (`Ctrl+Shift+X`)
3. Search for "WaddleAI"
4. Click "Install" on the "WaddleAI for Copilot Chat" extension
5. Reload VS Code when prompted

### Method 2: Install from VSIX File

If you have the `.vsix` package file:

1. Download the `waddleai-copilot-x.x.x.vsix` file
2. Open VS Code
3. Go to Extensions panel (`Ctrl+Shift+X`)
4. Click the `...` menu → "Install from VSIX..."
5. Select the downloaded VSIX file
6. Reload VS Code when prompted

### Method 3: Command Line Installation

```bash
code --install-extension waddleai-copilot-x.x.x.vsix
```

## Initial Configuration

### 1. Set Your API Key

After installation, you'll see a notification to set up your API key:

1. Click "Set API Key" in the notification, or
2. Use Command Palette (`Ctrl+Shift+P`) → "WaddleAI: Set API Key"
3. Enter your API key in the format: `wa-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`
4. The key will be securely stored in VS Code's secret storage

### 2. Configure Server Endpoint (if needed)

If you're using a custom WaddleAI deployment:

1. Open VS Code Settings (`Ctrl+,`)
2. Search for "WaddleAI"
3. Set `waddleai.apiEndpoint` to your server URL (e.g., `https://waddleai.yourcompany.com`)

### 3. Test Connection

Verify everything is working:

1. Open Command Palette (`Ctrl+Shift+P`)
2. Run "WaddleAI: Test Connection"
3. You should see a success message if configured correctly

### 4. Select Your Preferred Model

Choose which AI model to use by default:

1. Command Palette (`Ctrl+Shift+P`) → "WaddleAI: Select Model"
2. Choose from available models (GPT-4, Claude-3, Llama2, etc.)

## Using WaddleAI in Copilot Chat

### 1. Open Copilot Chat

- Use keyboard shortcut: `Ctrl+Shift+I`
- Or from Command Palette: "GitHub Copilot: Open Chat"

### 2. Select WaddleAI as Provider

1. In the chat interface, look for the model selector dropdown
2. Click the dropdown and select "WaddleAI"
3. Choose your specific model (if multiple are available)

### 3. Start Chatting

You can now chat with AI models through WaddleAI! The extension provides:

- **Context Awareness**: Automatically includes your selected code and workspace info
- **Memory Enhancement**: Remembers previous conversations and preferences
- **Security Scanning**: Protects against prompt injection attacks
- **Multi-Model Support**: Switch between different AI models seamlessly

## Advanced Configuration

### Settings Reference

Open VS Code Settings and search for "WaddleAI" to configure:

```json
{
  // Server Configuration
  "waddleai.apiEndpoint": "http://localhost:8000",
  "waddleai.apiKey": "",  // Stored securely, don't set directly

  // Model Configuration
  "waddleai.defaultModel": "gpt-4",
  "waddleai.maxTokens": 2048,
  "waddleai.temperature": 0.7,

  // Feature Toggles
  "waddleai.enableMemory": true,
  "waddleai.enableSecurityScanning": true
}
```

### Available Models

The extension supports various AI models:

- **OpenAI Models**: `gpt-4`, `gpt-3.5-turbo`
- **Anthropic Models**: `claude-3-opus-20240229`, `claude-3-sonnet-20240229`
- **Ollama Models**: `llama2`, `codellama`, `mistral`, etc.

Available models depend on your WaddleAI server configuration.

### Memory Features

When memory is enabled, WaddleAI remembers:

- Your coding style and preferences
- Project-specific context and patterns
- Previous conversation history
- Frequently used libraries and frameworks

To manage memory:

- **Clear Memory**: Command Palette → "WaddleAI: Clear Memory"
- **Disable Memory**: Set `waddleai.enableMemory` to `false`

### Security Features

WaddleAI includes built-in security scanning:

- **Prompt Injection Detection**: Blocks malicious prompt patterns
- **Jailbreak Prevention**: Prevents attempts to bypass AI safety
- **Data Protection**: Scans for potential data exposure

Toggle with: `waddleai.enableSecurityScanning`

## Troubleshooting

### Common Issues

#### "Authentication Failed"
- Double-check your API key format
- Verify the API key is active in your WaddleAI dashboard
- Check that your account has proper permissions

#### "Connection Failed"
- Ensure WaddleAI server is running and accessible
- Check firewall/proxy settings
- Verify the endpoint URL is correct

#### "Model Not Available"
- Check which models are enabled in your WaddleAI configuration
- Verify your API key has access to the selected model
- Try selecting a different model

#### Extension Not Appearing in Copilot Chat
- Ensure VS Code is version 1.85.0 or higher
- Restart VS Code after installation
- Check that the extension is enabled in Extensions panel

### Debug Mode

For troubleshooting, enable debug logging:

1. Open VS Code Settings
2. Add: `"waddleai.debug": true`
3. View logs: `View → Output → WaddleAI`

### Getting Help

- **Documentation**: Check the [WaddleAI documentation](https://docs.waddleai.com)
- **Support**: File issues at [GitHub Issues](https://github.com/penguintechinc/waddleai/issues)
- **Community**: Join our [Discord server](https://discord.gg/waddleai)

## Uninstallation

To remove the extension:

1. Go to Extensions panel (`Ctrl+Shift+X`)
2. Find "WaddleAI for Copilot Chat"
3. Click the gear icon → "Uninstall"
4. Optionally clear stored data:
   - Command Palette → "Developer: Reload Window"
   - This clears cached authentication data

## Next Steps

Once installed and configured:

1. Try asking coding questions in Copilot Chat
2. Experiment with different AI models
3. Monitor your token usage with "WaddleAI: Show Usage"
4. Explore advanced features like memory enhancement
5. Configure custom security policies if needed

Enjoy using WaddleAI with VS Code! 🎉
