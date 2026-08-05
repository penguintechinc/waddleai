/**
 * Generate Code Command
 * Generates code based on selection or comments using Ollama
 */

import * as vscode from 'vscode';
import { ollamaClient } from '../api/ollamaClient';
import { getConfiguration } from '../config/configuration';
import { buildGenerationPrompt } from '../utils/promptBuilder';
import { formatGeneratedCode } from '../utils/codeFormatter';
import { handleError, isOllamaConnectionError } from '../utils/errorHandler';

/**
 * Command to generate code based on context or comments
 * Supports selection or uses current line context
 */
export async function generateCodeCommand(): Promise<void> {
  try {
    // Get active editor
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      vscode.window.showWarningMessage('No active editor found');
      return;
    }

    // Check Ollama availability
    const isAvailable = await ollamaClient.isAvailable();
    if (!isAvailable) {
      vscode.window.showErrorMessage(
        'Ollama server is not available. Please ensure Ollama is running.'
      );
      return;
    }

    const document = editor.document;
    const selection = editor.selection;
    const languageId = document.languageId;

    // Get context: selection or current line
    let context = '';
    let insertPosition: vscode.Position;
    let replaceRange: vscode.Range | null = null;

    if (!selection.isEmpty) {
      // Use selection as context
      context = document.getText(selection);
      insertPosition = selection.end;
      replaceRange = selection;
    } else {
      // Use current line and look for comments
      const currentLine = document.lineAt(selection.start.line);
      context = currentLine.text.trim();

      // If the line is empty or doesn't look like a comment/instruction, get surrounding context
      if (!context || (!context.startsWith('//') && !context.startsWith('#') && !context.startsWith('/*'))) {
        const startLine = Math.max(0, selection.start.line - 5);
        const endLine = Math.min(document.lineCount - 1, selection.start.line + 5);
        const range = new vscode.Range(startLine, 0, endLine, document.lineAt(endLine).text.length);
        context = document.getText(range);
      }

      insertPosition = currentLine.range.end;
    }

    if (!context.trim()) {
      vscode.window.showWarningMessage('No context found. Please select code or write a comment describing what to generate.');
      return;
    }

    // Get configuration
    const config = getConfiguration();
    const model = config.defaultModel;

    // Build prompt
    const prompt = buildGenerationPrompt(context, languageId);

    // Generate code with progress notification
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: 'Generating code...',
        cancellable: true,
      },
      async (progress, token) => {
        let generatedCode = '';

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
            generatedCode += chunk;

            // Update progress with the generated content
            if (chunk) {
              progress.report({
                message: `Generating... (${generatedCode.length} chars)`
              });
            }
          }
        );

        // Format the generated code
        const formattedCode = formatGeneratedCode(generatedCode, languageId);

        if (!formattedCode) {
          vscode.window.showWarningMessage('No code was generated');
          return;
        }

        // Insert or replace the code
        await editor.edit((editBuilder) => {
          if (replaceRange) {
            // Replace selection
            editBuilder.replace(replaceRange, formattedCode);
          } else {
            // Insert at position with a newline
            const insertText = '\n' + formattedCode;
            editBuilder.insert(insertPosition, insertText);
          }
        });

        vscode.window.showInformationMessage('Code generated successfully');
      }
    );
  } catch (error) {
    if (isOllamaConnectionError(error)) {
      vscode.window.showErrorMessage(
        'Failed to connect to Ollama. Please ensure Ollama is running and accessible.'
      );
    } else {
      handleError(error, 'Generate Code');
    }
  }
}
