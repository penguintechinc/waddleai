/**
 * Extension Configuration
 * Reads and manages VS Code extension settings
 */

import * as vscode from 'vscode';

export interface OllamaConfiguration {
  apiUrl: string;
  defaultModel: string;
  chatModel: string;
  temperature: number;
  maxTokens: number;
  enableInlineCompletion: boolean;
  inlineCompletionDebounceMs: number;
  contextLines: number;
  timeout: number;
  supportedLanguages: string[];
  systemPrompt?: string;
}

const CONFIG_SECTION = 'ollamaAssistant';

/**
 * Get the current extension configuration
 */
export function getConfiguration(): OllamaConfiguration {
  const config = vscode.workspace.getConfiguration(CONFIG_SECTION);

  return {
    apiUrl: config.get<string>('apiUrl', 'http://localhost:11434'),
    defaultModel: config.get<string>('defaultModel', 'codellama'),
    chatModel: config.get<string>('chatModel', 'llama3.1'),
    temperature: config.get<number>('temperature', 0.7),
    maxTokens: config.get<number>('maxTokens', 2048),
    enableInlineCompletion: config.get<boolean>('enableInlineCompletion', true),
    inlineCompletionDebounceMs: config.get<number>('inlineCompletionDebounceMs', 500),
    contextLines: config.get<number>('contextLines', 50),
    timeout: config.get<number>('timeout', 30000),
    supportedLanguages: config.get<string[]>('supportedLanguages', [
      'typescript',
      'javascript',
      'typescriptreact',
      'javascriptreact',
      'python',
      'go',
      'rust',
      'java',
      'csharp',
      'c',
      'cpp',
      'php',
      'ruby',
      'swift',
      'kotlin',
    ]),
  };
}

/**
 * Update a configuration value
 */
export async function updateConfiguration<K extends keyof OllamaConfiguration>(
  key: K,
  value: OllamaConfiguration[K],
  target: vscode.ConfigurationTarget = vscode.ConfigurationTarget.Global
): Promise<void> {
  const config = vscode.workspace.getConfiguration(CONFIG_SECTION);
  await config.update(key, value, target);
}

/**
 * Check if a language is supported for inline completion
 */
export function isLanguageSupported(languageId: string): boolean {
  const config = getConfiguration();
  return config.supportedLanguages.includes(languageId);
}
