/**
 * Commands Index
 * Registers all extension commands
 */

import * as vscode from 'vscode';
import { generateCodeCommand } from './generateCode';
import { explainCodeCommand } from './explainCode';
import { refactorCodeCommand } from './refactorCode';
import { fixCodeCommand } from './fixCode';
import { ollamaClient } from '../api/ollamaClient';
import { getConfiguration, updateConfiguration } from '../config/configuration';
import { handleError } from '../utils/errorHandler';

// ChatViewProvider type (will be defined in providers/ChatViewProvider.ts)
export type ChatViewProvider = {
  clearHistory(): Promise<void>;
};

/**
 * Select Model Command
 * Shows a quick pick to select the active Ollama model
 */
async function selectModelCommand(): Promise<void> {
  try {
    // Check if Ollama is available
    const isAvailable = await ollamaClient.isAvailable();
    if (!isAvailable) {
      vscode.window.showErrorMessage(
        'Ollama server is not available. Please ensure Ollama is running.'
      );
      return;
    }

    // Fetch available models
    const models = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: 'Fetching models...',
        cancellable: false,
      },
      async () => {
        return await ollamaClient.listModels();
      }
    );

    if (!models || models.length === 0) {
      vscode.window.showWarningMessage('No models found. Please pull a model using Ollama CLI.');
      return;
    }

    // Get current model
    const config = getConfiguration();
    const currentModel = config.defaultModel;

    // Show quick pick
    const selected = await vscode.window.showQuickPick(
      models.map(model => ({
        label: model,
        description: model === currentModel ? '(current)' : '',
        picked: model === currentModel,
      })),
      {
        placeHolder: 'Select a model for code generation',
        title: 'Ollama Model Selection',
      }
    );

    if (selected) {
      // Update configuration
      await updateConfiguration('defaultModel', selected.label);
      vscode.window.showInformationMessage(`Model changed to: ${selected.label}`);
    }
  } catch (error) {
    handleError(error, 'Select Model');
  }
}

/**
 * Refresh Models Command
 * Refreshes the list of available models
 */
async function refreshModelsCommand(): Promise<void> {
  try {
    // Check if Ollama is available
    const isAvailable = await ollamaClient.isAvailable();
    if (!isAvailable) {
      vscode.window.showErrorMessage(
        'Ollama server is not available. Please ensure Ollama is running.'
      );
      return;
    }

    // Fetch models with progress
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: 'Refreshing models...',
        cancellable: false,
      },
      async () => {
        const models = await ollamaClient.listModels();
        vscode.window.showInformationMessage(
          `Found ${models.length} model(s): ${models.join(', ')}`
        );
      }
    );
  } catch (error) {
    handleError(error, 'Refresh Models');
  }
}

/**
 * Clear Chat Command
 * Clears the chat history in the chat view
 */
function createClearChatCommand(chatViewProvider: ChatViewProvider) {
  return async (): Promise<void> => {
    try {
      await chatViewProvider.clearHistory();
      vscode.window.showInformationMessage('Chat history cleared');
    } catch (error) {
      handleError(error, 'Clear Chat');
    }
  };
}

/**
 * Toggle Inline Completion Command
 * Enables or disables inline code completion
 */
async function toggleInlineCompletionCommand(): Promise<void> {
  try {
    const config = getConfiguration();
    const currentValue = config.enableInlineCompletion;
    const newValue = !currentValue;

    await updateConfiguration('enableInlineCompletion', newValue);

    vscode.window.showInformationMessage(
      `Inline completion ${newValue ? 'enabled' : 'disabled'}`
    );
  } catch (error) {
    handleError(error, 'Toggle Inline Completion');
  }
}

/**
 * Register all extension commands
 * @param context - The extension context
 * @param chatViewProvider - The chat view provider instance
 */
export function registerCommands(
  context: vscode.ExtensionContext,
  chatViewProvider: ChatViewProvider
): void {
  // Register code assistance commands
  context.subscriptions.push(
    vscode.commands.registerCommand('ollama-assistant.generateCode', generateCodeCommand)
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('ollama-assistant.explainCode', explainCodeCommand)
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('ollama-assistant.refactorCode', refactorCodeCommand)
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('ollama-assistant.fixCode', fixCodeCommand)
  );

  // Register model management commands
  context.subscriptions.push(
    vscode.commands.registerCommand('ollama-assistant.selectModel', selectModelCommand)
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('ollama-assistant.refreshModels', refreshModelsCommand)
  );

  // Register chat commands
  context.subscriptions.push(
    vscode.commands.registerCommand(
      'ollama-assistant.clearChat',
      createClearChatCommand(chatViewProvider)
    )
  );

  // Register settings commands
  context.subscriptions.push(
    vscode.commands.registerCommand(
      'ollama-assistant.toggleInlineCompletion',
      toggleInlineCompletionCommand
    )
  );
}
