# Change Log

All notable changes to the "WaddleAI for Copilot Chat" extension will be documented in this file.

## [0.2.0] - 2026-08-18

### Changed
- Refreshed `waddleaiClient.ts` to the routes the WaddleAI proxy actually
  serves today: chat completions now request `stream: false` and are read
  as one JSON envelope (the proxy has no `text/event-stream` response path
  for `/v1/chat/completions`, so the previous SSE parser never produced
  output against a real deployment); usage now combines `/api/usage` +
  `/api/quota` (there is no `/v1/usage` route); connection testing now
  calls `/readyz` (there is no `/health` route).
- `@waddleai` chat responses now surface the proxy's additive
  `usage.waddleai` cache/routing metadata (spec §6.4/§7.6) as a compact
  footer — cache hit type + tokens saved, `routed_from` cause, and
  knowledge-context token count — when the response carries it.
- "WaddleAI: Clear Conversation Memory" is now "WaddleAI: Start New
  Conversation": it resets the extension's own session id (which the
  proxy's memory layers key off via `X-WaddleAI-Session`) instead of
  calling a server-side "clear memory" route that doesn't exist.
- Pinned `axios`, `openai`, and all devDependencies to exact versions
  (no `^`/`~`) matching the committed lockfile.

### Removed
- `streamChatCompletion`/SSE parsing, `getHistory`, and `sendTelemetry`
  from `waddleaiClient.ts` — none had a backing proxy route.

## [0.1.0] - 2024-12-26

### Added
- Initial release of WaddleAI for VS Code Copilot Chat
- Integration with VS Code's language model API
- Support for multiple AI models (GPT-4, Claude-3, Llama2, etc.)
- Secure API key management with VS Code's secret storage
- Conversation memory enhancement for context-aware responses
- Built-in security scanning for prompt injection protection
- Token usage tracking and quota monitoring
- Intelligent model routing based on cost and performance
- Custom endpoint configuration for enterprise deployments
- Real-time streaming responses in Copilot Chat
- Automatic workspace context injection
- Commands for model selection and configuration management

### Features
- **Multi-LLM Support**: Access to OpenAI, Anthropic, and Ollama models
- **Memory Integration**: Conversation history and user preferences
- **Security First**: Prompt injection detection and prevention
- **Enterprise Ready**: Custom endpoints and authentication
- **Usage Analytics**: Token tracking and billing transparency
- **Smart Routing**: Automatic model selection optimization

### Commands
- `WaddleAI: Set API Key` - Securely configure authentication
- `WaddleAI: Select Model` - Choose your preferred AI model
- `WaddleAI: Test Connection` - Verify server connectivity
- `WaddleAI: Show Token Usage` - Monitor usage and quotas
- `WaddleAI: Clear Memory` - Reset conversation context

### Configuration Options
- `waddleai.apiEndpoint` - WaddleAI server URL
- `waddleai.apiKey` - Authentication key (stored securely)
- `waddleai.defaultModel` - Preferred AI model
- `waddleai.enableMemory` - Conversation memory toggle
- `waddleai.enableSecurityScanning` - Security features toggle
- `waddleai.maxTokens` - Response length limit
- `waddleai.temperature` - Response creativity control
