/**
 * Explain Code Command
 * Generates explanations for selected code using Ollama
 */

import * as vscode from 'vscode';
import { ollamaClient } from '../api/ollamaClient';
import { getConfiguration } from '../config/configuration';
import { buildExplanationPrompt } from '../utils/promptBuilder';
import { handleError, isOllamaConnectionError } from '../utils/errorHandler';

// Create a shared output channel for Ollama
let outputChannel: vscode.OutputChannel | null = null;

function getOutputChannel(): vscode.OutputChannel {
  if (!outputChannel) {
    outputChannel = vscode.window.createOutputChannel('Ollama');
  }
  return outputChannel;
}

/**
 * Command to explain selected code
 * Shows the explanation in the Ollama output channel
 */
export async function explainCodeCommand(): Promise<void> {
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
      vscode.window.showWarningMessage('Please select code to explain');
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
    const model = config.chatModel || config.defaultModel;

    // Build prompt
    const prompt = buildExplanationPrompt(selectedCode, languageId);

    // Get output channel and prepare it
    const output = getOutputChannel();
    output.clear();
    output.show(true);
    output.appendLine('='.repeat(80));
    output.appendLine('Code Explanation');
    output.appendLine('='.repeat(80));
    output.appendLine('');
    output.appendLine('Selected Code:');
    output.appendLine('```' + languageId);
    output.appendLine(selectedCode);
    output.appendLine('```');
    output.appendLine('');
    output.appendLine('Explanation:');
    output.appendLine('-'.repeat(80));

    // Generate explanation with progress
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: 'Explaining code...',
        cancellable: true,
      },
      async (_progress, token) => {
        let explanation = '';

        // Handle cancellation
        token.onCancellationRequested(() => {
          ollamaClient.cancelGeneration();
        });

        // Stream explanation
        await ollamaClient.generateStream(
          {
            model,
            prompt,
            options: {
              temperature: config.temperature,
              num_predict: config.maxTokens,
            },
          },
          (chunk: string, done: boolean) => {
            explanation += chunk;
            output.append(chunk);

            if (done) {
              output.appendLine('');
              output.appendLine('='.repeat(80));
              vscode.window.showInformationMessage('Code explanation complete');
            }
          }
        );
      }
    );
  } catch (error) {
    if (isOllamaConnectionError(error)) {
      vscode.window.showErrorMessage(
        'Failed to connect to Ollama. Please ensure Ollama is running and accessible.'
      );
    } else {
      handleError(error, 'Explain Code');
    }
  }
}
