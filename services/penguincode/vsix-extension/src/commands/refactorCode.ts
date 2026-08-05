/**
 * Refactor Code Command
 * Refactors selected code for improved quality and maintainability
 */

import * as vscode from 'vscode';
import { ollamaClient } from '../api/ollamaClient';
import { getConfiguration } from '../config/configuration';
import { buildRefactorPrompt } from '../utils/promptBuilder';
import { formatGeneratedCode } from '../utils/codeFormatter';
import { handleError, isOllamaConnectionError } from '../utils/errorHandler';

/**
 * Command to refactor selected code
 * Replaces the selection with the refactored version
 */
export async function refactorCodeCommand(): Promise<void> {
  try {
    // Get active editor
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      vscode.window.showWarningMessage('No active editor found');
      return;
    }

    // Get selected code
    const selection = editor.selection;
    if (selection.isEmpty) {
      vscode.window.showWarningMessage('Please select code to refactor');
      return;
    }

    const document = editor.document;
    const selectedCode = document.getText(selection);
    const languageId = document.languageId;

    // Check Ollama availability
    const isAvailable = await ollamaClient.isAvailable();
    if (!isAvailable) {
      vscode.window.showErrorMessage(
        'Ollama server is not available. Please ensure Ollama is running.'
      );
      return;
    }

    // Get configuration
    const config = getConfiguration();
    const model = config.defaultModel;

    // Build prompt
    const prompt = buildRefactorPrompt(selectedCode, languageId);

    // Generate refactored code with progress
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: 'Refactoring code...',
        cancellable: true,
      },
      async (progress, token) => {
        let refactoredCode = '';

        // Handle cancellation
        token.onCancellationRequested(() => {
          ollamaClient.cancelGeneration();
        });

        // Stream generation
        await ollamaClient.generateStream(
          {
            model,
            prompt,
            options: {
              temperature: config.temperature,
              num_predict: config.maxTokens,
            },
          },
          (chunk: string, _done: boolean) => {
            refactoredCode += chunk;

            if (chunk) {
              progress.report({
                message: `Refactoring... (${refactoredCode.length} chars)`
              });
            }
          }
        );

        // Format the refactored code
        const formattedCode = formatGeneratedCode(refactoredCode, languageId);

        if (!formattedCode) {
          vscode.window.showWarningMessage('No refactored code was generated');
          return;
        }

        // Show a preview and ask for confirmation
        const choice = await vscode.window.showInformationMessage(
          'Refactoring complete. Replace selected code?',
          'Replace',
          'Cancel'
        );

        if (choice === 'Replace') {
          // Replace the selection with refactored code
          await editor.edit((editBuilder) => {
            editBuilder.replace(selection, formattedCode);
          });

          vscode.window.showInformationMessage('Code refactored successfully');
        }
      }
    );
  } catch (error) {
    if (isOllamaConnectionError(error)) {
      vscode.window.showErrorMessage(
        'Failed to connect to Ollama. Please ensure Ollama is running and accessible.'
      );
    } else {
      handleError(error, 'Refactor Code');
    }
  }
}
