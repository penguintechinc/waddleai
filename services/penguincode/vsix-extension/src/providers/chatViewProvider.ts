import * as vscode from 'vscode';
import { ollamaClient } from '../api/ollamaClient';
import { ChatMessage, ChatState, WebviewMessage, StreamChunkPayload, ModelsLoadedPayload, ConnectionStatusPayload } from '../types/chat';
import { ModelInfo } from '../types/ollama';

export class ChatViewProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'ollamaAssistant.chatView';

    private webviewView?: vscode.WebviewView;
    private chatHistory: ChatMessage[] = [];
    private currentModel?: string;
    private availableModels: ModelInfo[] = [];
    private isGenerating: boolean = false;

    constructor(private readonly extensionUri: vscode.Uri) {}

    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        _context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken
    ): void | Thenable<void> {
        this.webviewView = webviewView;

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [this.extensionUri]
        };

        webviewView.webview.html = this.getHtmlForWebview(webviewView.webview);

        webviewView.webview.onDidReceiveMessage(
            message => this.handleWebviewMessage(message)
        );

        this.initialize();
    }

    private async initialize(): Promise<void> {
        try {
            console.log('Ollama Assistant: Initializing...');
            const isConnected = await ollamaClient.isAvailable();
            console.log('Ollama Assistant: Connection status:', isConnected);
            this.sendToWebview('connectionStatus', { connected: isConnected } as ConnectionStatusPayload);

            if (isConnected) {
                await this.loadModels();
            } else {
                console.log('Ollama Assistant: Not connected to Ollama');
            }
        } catch (error) {
            console.error('Ollama Assistant: Failed to initialize:', error);
            this.sendToWebview('connectionStatus', { connected: false } as ConnectionStatusPayload);
        }
    }

    private async handleWebviewMessage(message: WebviewMessage): Promise<void> {
        switch (message.type) {
            case 'sendMessage':
                if (message.content) {
                    await this.handleSendMessage(message.content);
                }
                break;

            case 'selectModel':
                if (message.model) {
                    this.currentModel = message.model;
                    this.sendStateUpdate();
                }
                break;

            case 'clearHistory':
                await this.clearHistory();
                break;

            case 'cancelGeneration':
                this.isGenerating = false;
                ollamaClient.cancelGeneration();
                this.sendStateUpdate();
                break;

            case 'refreshModels':
                await this.refreshModels();
                break;

            case 'getState':
                this.sendStateUpdate();
                break;

            case 'insertCode':
                if (message.code) {
                    await this.insertCodeToEditor(message.code);
                }
                break;
        }
    }

    private async handleSendMessage(content: string): Promise<void> {
        if (!this.currentModel) {
            vscode.window.showErrorMessage('Please select a model first');
            return;
        }

        if (this.isGenerating) {
            return;
        }

        const userMessage: ChatMessage = {
            id: this.generateId(),
            role: 'user',
            content,
            timestamp: Date.now()
        };

        this.chatHistory.push(userMessage);
        this.sendStateUpdate();

        const assistantMessage: ChatMessage = {
            id: this.generateId(),
            role: 'assistant',
            content: '',
            timestamp: Date.now()
        };

        this.chatHistory.push(assistantMessage);
        this.isGenerating = true;
        this.sendStateUpdate();

        try {
            await ollamaClient.chatStream(
                {
                    model: this.currentModel,
                    messages: this.chatHistory.slice(0, -1).map(msg => ({
                        role: msg.role,
                        content: msg.content
                    }))
                },
                (chunk: string, done: boolean) => {
                    if (!this.isGenerating) {
                        return;
                    }

                    assistantMessage.content += chunk;
                    this.sendToWebview('streamChunk', {
                        id: assistantMessage.id,
                        content: chunk,
                        done
                    } as StreamChunkPayload);
                }
            );

            this.chatHistory[this.chatHistory.length - 1] = assistantMessage;
        } catch (error) {
            const errorMessage = error instanceof Error ? error.message : 'Unknown error occurred';
            assistantMessage.content = `Error: ${errorMessage}`;
            this.chatHistory[this.chatHistory.length - 1] = assistantMessage;
            vscode.window.showErrorMessage(`Chat error: ${errorMessage}`);
        } finally {
            this.isGenerating = false;
            this.sendStateUpdate();
        }
    }

    private async insertCodeToEditor(code: string): Promise<void> {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            vscode.window.showErrorMessage('No active editor found');
            return;
        }

        await editor.edit(editBuilder => {
            editBuilder.insert(editor.selection.active, code);
        });
    }

    private sendStateUpdate(): void {
        const state: ChatState = {
            messages: this.chatHistory,
            currentModel: this.currentModel ?? '',
            availableModels: this.availableModels,
            isLoading: this.isGenerating,
            isConnected: true
        };
        this.sendToWebview('stateUpdate', state);
    }

    private sendToWebview(type: string, payload: any): void {
        if (this.webviewView) {
            this.webviewView.webview.postMessage({ type, ...payload });
        }
    }

    private generateId(): string {
        return `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
    }

    private async loadModels(): Promise<void> {
        try {
            console.log('Ollama Assistant: Loading models with capabilities...');
            const models = await ollamaClient.listModelsWithInfo();
            console.log('Ollama Assistant: Models found:', models);
            this.availableModels = models;

            if (models.length > 0 && !this.currentModel) {
                this.currentModel = models[0].name;
            }

            console.log('Ollama Assistant: Sending modelsLoaded to webview');
            this.sendToWebview('modelsLoaded', {
                models: this.availableModels,
                current: this.currentModel ?? ''
            } as ModelsLoadedPayload);

            this.sendStateUpdate();
        } catch (error) {
            console.error('Ollama Assistant: Failed to load models:', error);
            vscode.window.showErrorMessage('Failed to load Ollama models');
        }
    }

    public async clearHistory(): Promise<void> {
        this.chatHistory = [];
        this.sendStateUpdate();
    }

    public async clearChat(): Promise<void> {
        await this.clearHistory();
    }

    public async refreshModels(): Promise<void> {
        await this.initialize();
    }

    private getHtmlForWebview(webview: vscode.Webview): string {
        const nonce = this.getNonce();

        return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
    <title>Ollama Assistant</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: var(--vscode-font-family);
            font-size: var(--vscode-font-size);
            color: var(--vscode-foreground);
            background-color: var(--vscode-sideBar-background);
            display: flex;
            flex-direction: column;
            height: 100vh;
            overflow: hidden;
        }

        .header {
            padding: 10px;
            border-bottom: 1px solid var(--vscode-panel-border);
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .model-controls {
            display: flex;
            gap: 8px;
            align-items: center;
        }

        select {
            flex: 1;
            background-color: var(--vscode-dropdown-background);
            color: var(--vscode-dropdown-foreground);
            border: 1px solid var(--vscode-dropdown-border);
            padding: 4px 8px;
            cursor: pointer;
        }

        button {
            background-color: var(--vscode-button-background);
            color: var(--vscode-button-foreground);
            border: none;
            padding: 4px 12px;
            cursor: pointer;
            white-space: nowrap;
        }

        button:hover {
            background-color: var(--vscode-button-hoverBackground);
        }

        button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        button.secondary {
            background-color: var(--vscode-button-secondaryBackground);
            color: var(--vscode-button-secondaryForeground);
        }

        button.secondary:hover {
            background-color: var(--vscode-button-secondaryHoverBackground);
        }

        .messages-container {
            flex: 1;
            overflow-y: auto;
            padding: 10px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        .message {
            padding: 10px;
            border-radius: 4px;
            white-space: pre-wrap;
            word-wrap: break-word;
        }

        .message.user {
            background-color: var(--vscode-input-background);
            border-left: 3px solid var(--vscode-focusBorder);
        }

        .message.assistant {
            background-color: var(--vscode-editor-background);
            border-left: 3px solid var(--vscode-textLink-foreground);
        }

        .message-header {
            font-weight: bold;
            margin-bottom: 6px;
            font-size: 0.9em;
            opacity: 0.8;
        }

        .message-content {
            line-height: 1.5;
        }

        .message-content code {
            background-color: var(--vscode-textCodeBlock-background);
            padding: 2px 4px;
            border-radius: 3px;
            font-family: var(--vscode-editor-font-family);
        }

        .message-content pre {
            background-color: var(--vscode-textCodeBlock-background);
            padding: 8px;
            border-radius: 4px;
            overflow-x: auto;
            margin: 8px 0;
        }

        .message-content pre code {
            padding: 0;
            background: none;
        }

        .input-area {
            padding: 10px;
            border-top: 1px solid var(--vscode-panel-border);
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        textarea {
            width: 100%;
            min-height: 60px;
            max-height: 150px;
            background-color: var(--vscode-input-background);
            color: var(--vscode-input-foreground);
            border: 1px solid var(--vscode-input-border);
            padding: 8px;
            resize: vertical;
            font-family: inherit;
            font-size: inherit;
        }

        textarea:focus {
            outline: 1px solid var(--vscode-focusBorder);
        }

        .input-buttons {
            display: flex;
            gap: 8px;
            justify-content: flex-end;
        }

        .status-message {
            padding: 10px;
            text-align: center;
            opacity: 0.7;
            font-style: italic;
        }

        .empty-state {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-direction: column;
            gap: 12px;
            opacity: 0.6;
            padding: 20px;
            text-align: center;
        }

        .capability-legend {
            display: flex;
            gap: 12px;
            font-size: 0.8em;
            opacity: 0.7;
            flex-wrap: wrap;
        }

        .capability-legend span {
            display: flex;
            align-items: center;
            gap: 3px;
        }

        .code-block-wrapper {
            position: relative;
            margin: 8px 0;
        }

        .code-insert-button {
            position: absolute;
            top: 4px;
            right: 4px;
            padding: 2px 8px;
            font-size: 0.85em;
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="model-controls">
            <select id="modelSelect" disabled>
                <option value="">Loading models...</option>
            </select>
            <button id="refreshButton" class="secondary">Refresh</button>
        </div>
        <div class="capability-legend">
            <span>💻 Code</span>
            <span>🔧 Tools</span>
            <span>👁 Vision</span>
        </div>
        <div class="model-controls">
            <button id="clearButton" class="secondary">Clear History</button>
        </div>
    </div>

    <div class="messages-container" id="messagesContainer">
        <div class="empty-state">
            <div>Start a conversation with Ollama</div>
            <div style="font-size: 0.9em;">Select a model and send a message to begin</div>
        </div>
    </div>

    <div class="input-area">
        <textarea id="messageInput" placeholder="Type your message here..." disabled></textarea>
        <div class="input-buttons">
            <button id="cancelButton" class="secondary" style="display: none;">Cancel</button>
            <button id="sendButton" disabled>Send</button>
        </div>
    </div>

    <script nonce="${nonce}">
        const vscode = acquireVsCodeApi();

        let state = {
            messages: [],
            currentModel: null,
            isGenerating: false,
            availableModels: []
        };

        const modelSelect = document.getElementById('modelSelect');
        const messageInput = document.getElementById('messageInput');
        const sendButton = document.getElementById('sendButton');
        const cancelButton = document.getElementById('cancelButton');
        const clearButton = document.getElementById('clearButton');
        const refreshButton = document.getElementById('refreshButton');
        const messagesContainer = document.getElementById('messagesContainer');

        // Request initial state
        vscode.postMessage({ type: 'getState' });

        // Handle messages from extension
        window.addEventListener('message', event => {
            const message = event.data;
            console.log('Ollama Webview received:', message.type, message);

            switch (message.type) {
                case 'stateUpdate':
                    state.messages = message.messages || [];
                    state.currentModel = message.currentModel;
                    state.availableModels = message.availableModels || state.availableModels;
                    state.isGenerating = message.isLoading || false;
                    updateUI();
                    updateModelSelect();
                    break;

                case 'modelsLoaded':
                    state.availableModels = message.models || [];
                    if (message.current) {
                        state.currentModel = message.current;
                    }
                    updateModelSelect();
                    break;

                case 'streamChunk':
                    updateMessageContent(message.id, message.content);
                    break;

                case 'connectionStatus':
                    updateConnectionStatus(message.connected);
                    break;
            }
        });

        function updateUI() {
            renderMessages();
            updateControls();
        }

        function renderMessages() {
            if (state.messages.length === 0) {
                messagesContainer.innerHTML = \`
                    <div class="empty-state">
                        <div>Start a conversation with Ollama</div>
                        <div style="font-size: 0.9em;">Select a model and send a message to begin</div>
                    </div>
                \`;
                return;
            }

            messagesContainer.innerHTML = state.messages.map(msg => {
                const content = formatMessageContent(msg.content);
                return \`
                    <div class="message \${msg.role}" data-message-id="\${msg.id}">
                        <div class="message-header">\${msg.role === 'user' ? 'You' : 'Assistant'}</div>
                        <div class="message-content">\${content}</div>
                    </div>
                \`;
            }).join('');

            messagesContainer.scrollTop = messagesContainer.scrollHeight;
        }

        function formatMessageContent(content) {
            // Basic markdown-like formatting
            let formatted = content
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;');

            // Code blocks
            formatted = formatted.replace(/\`\`\`([\\s\\S]*?)\`\`\`/g, (match, code) => {
                const codeId = 'code-' + Math.random().toString(36).substr(2, 9);
                return \`<div class="code-block-wrapper">
                    <button class="code-insert-button" onclick="insertCode('\${codeId}')">Insert</button>
                    <pre><code id="\${codeId}">\${code.trim()}</code></pre>
                </div>\`;
            });

            // Inline code
            formatted = formatted.replace(/\`([^\`]+)\`/g, '<code>$1</code>');

            return formatted;
        }

        function updateMessageContent(messageId, chunk) {
            const messageEl = document.querySelector(\`[data-message-id="\${messageId}"] .message-content\`);
            if (messageEl) {
                const message = state.messages.find(m => m.id === messageId);
                if (message) {
                    message.content += chunk;
                    messageEl.innerHTML = formatMessageContent(message.content);
                    messagesContainer.scrollTop = messagesContainer.scrollHeight;
                }
            }
        }

        function updateControls() {
            const hasModel = !!state.currentModel;
            const canSend = hasModel && !state.isGenerating && messageInput.value.trim().length > 0;

            messageInput.disabled = !hasModel || state.isGenerating;
            sendButton.disabled = !canSend;
            sendButton.style.display = state.isGenerating ? 'none' : 'block';
            cancelButton.style.display = state.isGenerating ? 'block' : 'none';
            modelSelect.disabled = state.isGenerating;
        }

        function formatModelOption(model) {
            // Handle both string and ModelInfo objects
            if (typeof model === 'string') {
                return { name: model, display: model };
            }

            const badges = [];
            if (model.capabilities) {
                if (model.capabilities.code) badges.push('💻');
                if (model.capabilities.tools) badges.push('🔧');
                if (model.capabilities.vision) badges.push('👁');
            }

            const badgeStr = badges.length > 0 ? ' ' + badges.join('') : '';
            const sizeStr = model.size && model.size !== 'Unknown' ? ' (' + model.size + ')' : '';

            return {
                name: model.name,
                display: model.name + sizeStr + badgeStr
            };
        }

        function updateModelSelect() {
            if (state.availableModels.length === 0) {
                modelSelect.innerHTML = '<option value="">No models available</option>';
                modelSelect.disabled = true;
                return;
            }

            modelSelect.innerHTML = state.availableModels.map(model => {
                const formatted = formatModelOption(model);
                return \`<option value="\${formatted.name}" \${formatted.name === state.currentModel ? 'selected' : ''}>\${formatted.display}</option>\`;
            }).join('');
            modelSelect.disabled = false;
        }

        function updateConnectionStatus(isConnected) {
            if (!isConnected) {
                messagesContainer.innerHTML = \`
                    <div class="status-message">
                        Unable to connect to Ollama. Please ensure Ollama is running.
                    </div>
                \`;
            }
        }

        window.insertCode = function(codeId) {
            const codeEl = document.getElementById(codeId);
            if (codeEl) {
                vscode.postMessage({
                    type: 'insertCode',
                    code: codeEl.textContent
                });
            }
        };

        // Event listeners
        sendButton.addEventListener('click', () => {
            const content = messageInput.value.trim();
            if (content) {
                vscode.postMessage({
                    type: 'sendMessage',
                    content
                });
                messageInput.value = '';
                updateControls();
            }
        });

        cancelButton.addEventListener('click', () => {
            vscode.postMessage({ type: 'cancelGeneration' });
        });

        clearButton.addEventListener('click', () => {
            vscode.postMessage({ type: 'clearHistory' });
        });

        refreshButton.addEventListener('click', () => {
            vscode.postMessage({ type: 'refreshModels' });
        });

        modelSelect.addEventListener('change', () => {
            vscode.postMessage({
                type: 'selectModel',
                model: modelSelect.value
            });
        });

        messageInput.addEventListener('input', () => {
            updateControls();
        });

        messageInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                if (!sendButton.disabled) {
                    sendButton.click();
                }
            }
        });
    </script>
</body>
</html>`;
    }

    private getNonce(): string {
        let text = '';
        const possible = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
        for (let i = 0; i < 32; i++) {
            text += possible.charAt(Math.floor(Math.random() * possible.length));
        }
        return text;
    }
}
