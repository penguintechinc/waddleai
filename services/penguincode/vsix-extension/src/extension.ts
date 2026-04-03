/**
 * Ollama Assistant Extension
 * Main entry point for the VS Code extension
 */

import * as vscode from 'vscode';
import { ChatViewProvider } from './providers/chatViewProvider';
import { InlineCompletionProvider } from './providers/inlineCompletionProvider';
import { registerOllamaLanguageModelProvider } from './providers/ollamaLanguageModelProvider';
import { registerCommands } from './commands';
import { getConfiguration } from './config/configuration';
import { ollamaClient } from './api/ollamaClient';

let chatViewProvider: ChatViewProvider;
let statusBarItem: vscode.StatusBarItem;

/**
 * Extension activation
 */
export async function activate(context: vscode.ExtensionContext): Promise<void> {
  console.log('Ollama Assistant is activating...');

  // Create status bar item
  statusBarItem = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right,
    100
  );
  statusBarItem.command = 'ollamaAssistant.selectModel';
  context.subscriptions.push(statusBarItem);

  // Initialize Chat View Provider
  chatViewProvider = new ChatViewProvider(context.extensionUri);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(
      ChatViewProvider.viewType,
      chatViewProvider,
      { webviewOptions: { retainContextWhenHidden: true } }
    )
  );

  // Initialize Inline Completion Provider
  const config = getConfiguration();
  if (config.enableInlineCompletion) {
    registerInlineCompletionProvider(context, config.supportedLanguages);
  }

  // Initialize config tracking for reload detection
  previousConfig = {
    enableInlineCompletion: config.enableInlineCompletion,
    supportedLanguages: [...config.supportedLanguages],
  };

  // Register commands
  registerCommands(context, chatViewProvider);

  // Register Ollama as a VS Code Language Model Provider
  // This enables Ollama models to appear in the Language Models panel
  // with proper capabilities for Agent and Plan modes
  registerOllamaLanguageModelProvider(context);

  // Register manage models command
  context.subscriptions.push(
    vscode.commands.registerCommand('ollamaAssistant.manageModels', async () => {
      // Open the Language Models panel
      await vscode.commands.executeCommand('workbench.action.chat.openLanguageModels');
    })
  );

  // Check Ollama availability on startup
  await checkOllamaConnection();

  // Listen for configuration changes
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration('ollamaAssistant')) {
        void handleConfigurationChange(context);
      }
    })
  );

  console.log('Ollama Assistant is now active');
}

/**
 * Register the inline completion provider for supported languages
 */
function registerInlineCompletionProvider(
  context: vscode.ExtensionContext,
  languages: string[]
): void {
  const inlineProvider = new InlineCompletionProvider();
  const selector = languages.map((lang) => ({ language: lang, scheme: 'file' }));

  context.subscriptions.push(
    vscode.languages.registerInlineCompletionItemProvider(selector, inlineProvider)
  );
}

/**
 * Check if Ollama is available and update status bar
 */
async function checkOllamaConnection(): Promise<void> {
  const isAvailable = await ollamaClient.isAvailable();

  if (isAvailable) {
    const config = getConfiguration();
    statusBarItem.text = `$(hubot) ${config.defaultModel}`;
    statusBarItem.tooltip = 'Ollama Assistant - Click to change model';
    statusBarItem.backgroundColor = undefined;
    statusBarItem.show();

    // Try to get models to verify full connectivity
    try {
      const models = await ollamaClient.listModels();
      if (models.length === 0) {
        statusBarItem.text = '$(hubot) No models';
        statusBarItem.tooltip = 'Ollama is running but no models are installed. Run: ollama pull codellama';
        statusBarItem.backgroundColor = new vscode.ThemeColor(
          'statusBarItem.warningBackground'
        );
      }
    } catch {
      // Silently ignore - basic connection is working
    }
  } else {
    statusBarItem.text = '$(hubot) Ollama offline';
    statusBarItem.tooltip = 'Ollama is not running. Click to retry.';
    statusBarItem.backgroundColor = new vscode.ThemeColor(
      'statusBarItem.errorBackground'
    );
    statusBarItem.show();

    // Show warning message with action
    const action = await vscode.window.showWarningMessage(
      'Ollama is not running. Some features will be unavailable.',
      'Retry',
      'Install Ollama',
      'Dismiss'
    );

    if (action === 'Retry') {
      await checkOllamaConnection();
    } else if (action === 'Install Ollama') {
      await vscode.env.openExternal(vscode.Uri.parse('https://ollama.ai'));
    }
  }
}

/** Track settings that require reload */
let previousConfig: {
  enableInlineCompletion: boolean;
  supportedLanguages: string[];
} | null = null;

/**
 * Handle configuration changes
 */
async function handleConfigurationChange(_context: vscode.ExtensionContext): Promise<void> {
  const config = getConfiguration();

  // Update status bar with new model
  if (statusBarItem) {
    statusBarItem.text = `$(hubot) ${config.defaultModel}`;
  }

  // Check if reload is required for certain settings
  const requiresReload = previousConfig !== null && (
    previousConfig.enableInlineCompletion !== config.enableInlineCompletion ||
    JSON.stringify(previousConfig.supportedLanguages) !== JSON.stringify(config.supportedLanguages)
  );

  // Update tracked config
  previousConfig = {
    enableInlineCompletion: config.enableInlineCompletion,
    supportedLanguages: [...config.supportedLanguages],
  };

  if (requiresReload) {
    const action = await vscode.window.showInformationMessage(
      'Ollama Assistant: Some settings require a reload to take effect.',
      'Reload Window',
      'Later'
    );

    if (action === 'Reload Window') {
      await vscode.commands.executeCommand('workbench.action.reloadWindow');
    }
  } else {
    vscode.window.showInformationMessage(
      'Ollama Assistant configuration updated.'
    );
  }
}

/**
 * Extension deactivation
 */
export function deactivate(): void {
  ollamaClient.cancelGeneration();
  if (statusBarItem) {
    statusBarItem.dispose();
  }
  console.log('Ollama Assistant deactivated');
}
