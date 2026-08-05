/**
 * Fix Code Command
 * Fixes bugs and issues in selected code using Ollama
 */

import * as vscode from 'vscode';
import { ollamaClient } from '../api/ollamaClient';
import { getConfiguration } from '../config/configuration';
import { buildFixPrompt } from '../utils/promptBuilder';
import { formatGeneratedCode } from '../utils/codeFormatter';
import { handleError, isOllamaConnectionError } from '../utils/errorHandler';

/**
 * Command to fix selected code
 * Optionally uses diagnostic errors to provide context
 * Replaces the selection with the fixed version
 */
export async function fixCodeCommand(): Promise<void> {
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
      vscode.window.showWarningMessage('Please select code to fix');
      return;
    }

    const document = editor.document;
    const selectedCode = document.getText(selection);
    const languageId = document.languageId;

    // Get diagnostic errors for the selected code
    let errorContext: string | undefined;
    const diagnostics = vscode.languages.getDiagnostics(document.uri);

    if (diagnostics && diagnostics.length > 0) {
      // Find diagnostics that overlap with the selection
      const relevantDiagnostics = diagnostics.filter(diagnostic =>
        diagnostic.range.intersection(selection)
      );

      if (relevantDiagnostics.length > 0) {
        // Format diagnostics into error context
        errorContext = relevantDiagnostics
          .map(diagnostic => {
            const severity = diagnostic.severity === vscode.DiagnosticSeverity.Error
              ? 'Error'
              : diagnostic.severity === vscode.DiagnosticSeverity.Warning
                ? 'Warning'
                : 'Info';
            return `[${severity}] Line ${diagnostic.range.start.line + 1}: ${diagnostic.message}`;
          })
          .join('\n');
      }
    }

    // If no diagnostics found, optionally ask the user for error description
    if (!errorContext) {
      const userError = await vscode.window.showInputBox({
        prompt: 'Describe the issue to fix (optional)',
        placeHolder: 'e.g., "Fix the null pointer exception" or leave empty',
      });

      if (userError !== undefined && userError.trim()) {
        errorContext = userError;
      }
    }

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
    const prompt = buildFixPrompt(selectedCode, languageId, errorContext);

    // Generate fixed code with progress
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: 'Fixing code...',
        cancellable: true,
      },
      async (progress, token) => {
        let fixedCode = '';

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
            fixedCode += chunk;

            if (chunk) {
              progress.report({
                message: `Fixing... (${fixedCode.length} chars)`
              });
            }
          }
        );

        // Format the fixed code
        const formattedCode = formatGeneratedCode(fixedCode, languageId);

        if (!formattedCode) {
          vscode.window.showWarningMessage('No fixed code was generated');
          return;
        }

        // Show a preview and ask for confirmation
        const choice = await vscode.window.showInformationMessage(
          'Fix complete. Replace selected code?',
          'Replace',
          'Cancel'
        );

        if (choice === 'Replace') {
          // Replace the selection with fixed code
          await editor.edit((editBuilder) => {
            editBuilder.replace(selection, formattedCode);
          });

          vscode.window.showInformationMessage('Code fixed successfully');
        }
      }
    );
  } catch (error) {
    if (isOllamaConnectionError(error)) {
      vscode.window.showErrorMessage(
        'Failed to connect to Ollama. Please ensure Ollama is running and accessible.'
      );
    } else {
      handleError(error, 'Fix Code');
    }
  }
}
