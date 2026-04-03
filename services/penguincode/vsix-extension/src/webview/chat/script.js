/**
 * Ollama Chat Webview Script
 * Handles client-side logic for the chat interface
 */

// @ts-check

(function () {
  // @ts-ignore
  const vscode = acquireVsCodeApi();

  // State
  let state = {
    messages: [],
    currentModel: '',
    availableModels: [],
    isLoading: false,
    isConnected: false,
  };

  // DOM Elements
  const messagesContainer = document.getElementById('messages');
  const messageInput = document.getElementById('message-input');
  const sendBtn = document.getElementById('send-btn');
  const cancelBtn = document.getElementById('cancel-btn');
  const modelSelect = document.getElementById('model-select');
  const refreshBtn = document.getElementById('refresh-btn');
  const clearBtn = document.getElementById('clear-btn');
  const connectionStatus = document.getElementById('connection-status');

  // Initialize
  function init() {
    // Request initial state
    vscode.postMessage({ type: 'getState' });

    // Event listeners
    sendBtn.addEventListener('click', sendMessage);
    cancelBtn.addEventListener('click', cancelGeneration);
    refreshBtn.addEventListener('click', refreshModels);
    clearBtn.addEventListener('click', clearHistory);
    modelSelect.addEventListener('change', selectModel);

    messageInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });

    // Auto-resize textarea
    messageInput.addEventListener('input', () => {
      messageInput.style.height = 'auto';
      messageInput.style.height = Math.min(messageInput.scrollHeight, 150) + 'px';
    });
  }

  // Send message
  function sendMessage() {
    const content = messageInput.value.trim();
    if (!content || state.isLoading) return;

    vscode.postMessage({
      type: 'sendMessage',
      payload: content,
    });

    messageInput.value = '';
    messageInput.style.height = 'auto';
  }

  // Cancel generation
  function cancelGeneration() {
    vscode.postMessage({ type: 'cancelGeneration' });
  }

  // Refresh models
  function refreshModels() {
    vscode.postMessage({ type: 'refreshModels' });
  }

  // Clear history
  function clearHistory() {
    vscode.postMessage({ type: 'clearHistory' });
  }

  // Select model
  function selectModel() {
    const model = modelSelect.value;
    vscode.postMessage({
      type: 'selectModel',
      payload: model,
    });
  }

  // Insert code to editor
  function insertCode(code) {
    vscode.postMessage({
      type: 'insertCode',
      payload: code,
    });
  }

  // Copy code to clipboard
  function copyCode(code) {
    navigator.clipboard.writeText(code).then(() => {
      // Show brief feedback
      const btn = event.target;
      const originalText = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(() => {
        btn.textContent = originalText;
      }, 1000);
    });
  }

  // Update UI based on state
  function updateUI() {
    // Update model select
    modelSelect.innerHTML = state.availableModels
      .map(
        (model) =>
          `<option value="${model}" ${model === state.currentModel ? 'selected' : ''}>${model}</option>`
      )
      .join('');

    // Update connection status
    if (!state.isConnected) {
      connectionStatus.textContent =
        'Ollama is not running. Please start Ollama and click Refresh.';
      connectionStatus.classList.add('error');
    } else {
      connectionStatus.classList.remove('error');
    }

    // Update buttons
    sendBtn.disabled = state.isLoading || !state.isConnected;
    cancelBtn.classList.toggle('hidden', !state.isLoading);
    sendBtn.classList.toggle('hidden', state.isLoading);

    // Render messages
    renderMessages();
  }

  // Render messages
  function renderMessages() {
    if (state.messages.length === 0) {
      messagesContainer.innerHTML = `
        <div class="empty-state">
          <h3>Ollama Assistant</h3>
          <p>Ask questions, generate code, or get help with your projects.</p>
          <p>Try: "Write a function to sort an array" or "Explain this code"</p>
        </div>
      `;
      return;
    }

    messagesContainer.innerHTML = state.messages
      .map((msg) => renderMessage(msg))
      .join('');

    // Scroll to bottom
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
  }

  // Render a single message
  function renderMessage(msg) {
    const roleClass = msg.role;
    const errorClass = msg.error ? 'error' : '';
    const content = formatMessageContent(msg.content);

    let modelBadge = '';
    if (msg.role === 'assistant' && msg.model) {
      modelBadge = `<div class="model-badge">${msg.model}</div>`;
    }

    let streamingIndicator = '';
    if (msg.isStreaming) {
      streamingIndicator = '<span class="streaming-indicator"></span>';
    }

    return `
      <div class="message ${roleClass} ${errorClass}" data-id="${msg.id}">
        <div class="message-content">${content}${streamingIndicator}</div>
        ${modelBadge}
      </div>
    `;
  }

  // Format message content with code blocks
  function formatMessageContent(content) {
    if (!content) return '';

    // Escape HTML
    let escaped = escapeHtml(content);

    // Format code blocks
    escaped = escaped.replace(
      /```(\w*)\n([\s\S]*?)```/g,
      (match, lang, code) => {
        const escapedCode = code.trim();
        return `<pre><code class="language-${lang || 'text'}">${escapedCode}</code><div class="code-actions"><button class="code-action-btn" onclick="copyCode(\`${escapedCode.replace(/`/g, '\\`').replace(/\$/g, '\\$')}\`)">Copy</button><button class="code-action-btn" onclick="insertCode(\`${escapedCode.replace(/`/g, '\\`').replace(/\$/g, '\\$')}\`)">Insert</button></div></pre>`;
      }
    );

    // Format inline code
    escaped = escaped.replace(/`([^`]+)`/g, '<code>$1</code>');

    // Convert newlines to <br> (outside of pre blocks)
    escaped = escaped.replace(/\n/g, '<br>');

    return escaped;
  }

  // Escape HTML
  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  // Handle messages from extension
  window.addEventListener('message', (event) => {
    const message = event.data;

    switch (message.type) {
      case 'stateUpdate':
        state = { ...state, ...message.payload };
        updateUI();
        break;

      case 'streamChunk':
        handleStreamChunk(message.payload);
        break;

      case 'streamEnd':
        handleStreamEnd(message.payload);
        break;

      case 'modelsLoaded':
        state.availableModels = message.payload.models;
        state.currentModel = message.payload.current;
        state.isConnected = true;
        updateUI();
        break;

      case 'connectionStatus':
        state.isConnected = message.payload.connected;
        updateUI();
        break;

      case 'error':
        showError(message.payload.message);
        break;
    }
  });

  // Handle streaming chunk
  function handleStreamChunk(payload) {
    const { id, content, done } = payload;

    // Find and update the message
    const msgIndex = state.messages.findIndex((m) => m.id === id);
    if (msgIndex !== -1) {
      state.messages[msgIndex].content = content;
      state.messages[msgIndex].isStreaming = !done;

      // Update just the message element for performance
      const msgElement = messagesContainer.querySelector(`[data-id="${id}"]`);
      if (msgElement) {
        const contentEl = msgElement.querySelector('.message-content');
        if (contentEl) {
          contentEl.innerHTML =
            formatMessageContent(content) +
            (!done ? '<span class="streaming-indicator"></span>' : '');
        }
      }

      // Scroll to bottom
      messagesContainer.scrollTop = messagesContainer.scrollHeight;
    }
  }

  // Handle stream end
  function handleStreamEnd(payload) {
    state.isLoading = false;
    const msgIndex = state.messages.findIndex((m) => m.id === payload.id);
    if (msgIndex !== -1) {
      state.messages[msgIndex].isStreaming = false;
    }
    updateUI();
  }

  // Show error
  function showError(message) {
    connectionStatus.textContent = message;
    connectionStatus.classList.add('error');
    state.isLoading = false;
    updateUI();
  }

  // Make functions globally available for onclick handlers
  window.insertCode = insertCode;
  window.copyCode = copyCode;

  // Initialize on load
  init();
})();
