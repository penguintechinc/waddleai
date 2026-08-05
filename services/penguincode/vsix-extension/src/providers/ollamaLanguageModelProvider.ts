/**
 * Ollama Language Model Provider
 * Implements VS Code's Language Model Chat Provider API to register Ollama models
 * in the system-level Language Models panel with proper capabilities.
 */

import * as vscode from 'vscode';
import { ollamaClient } from '../api/ollamaClient';
import { getConfiguration } from '../config/configuration';
import { ModelInfo } from '../types/ollama';

/**
 * Provider that registers Ollama models with VS Code's Language Model API
 */
export class OllamaLanguageModelProvider implements vscode.LanguageModelChatProvider {
  private cachedModels: ModelInfo[] = [];
  private lastFetch: number = 0;
  private readonly CACHE_TTL = 30000; // 30 seconds

  /**
   * Provide information about available language models
   */
  async provideLanguageModelChatInformation(
    _options: vscode.PrepareLanguageModelChatModelOptions,
    _token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelChatInformation[]> {
    console.log('Ollama LM Provider: provideLanguageModelChatInformation called');

    // Refresh cache if stale
    if (Date.now() - this.lastFetch > this.CACHE_TTL) {
      try {
        this.cachedModels = await ollamaClient.listModelsWithInfo();
        this.lastFetch = Date.now();
        console.log('Ollama LM Provider: Fetched models:', this.cachedModels.map(m => ({
          name: m.name,
          capabilities: m.capabilities
        })));
      } catch (error) {
        console.error('Ollama: Failed to fetch models:', error);
        // Return cached models if fetch fails
        if (this.cachedModels.length === 0) {
          return [];
        }
      }
    }

    const result = this.cachedModels.map((model) => this.toLanguageModelInfo(model));
    console.log('Ollama LM Provider: Returning models:', result.map(m => ({
      id: m.id,
      name: m.name,
      capabilities: m.capabilities
    })));
    return result;
  }

  /**
   * Convert ModelInfo to VS Code's LanguageModelChatInformation
   */
  private toLanguageModelInfo(model: ModelInfo): vscode.LanguageModelChatInformation {
    const config = getConfiguration();

    // Parse context size from model name if available (e.g., "llama3.1:8b-128k")
    const contextMatch = model.name.match(/(\d+)k/i);
    const contextSize = contextMatch ? parseInt(contextMatch[1]) * 1024 : 4096;

    return {
      id: `ollama:${model.name}`,
      name: model.name,
      family: model.family || 'ollama',
      version: '1.0',
      maxInputTokens: contextSize,
      maxOutputTokens: config.maxTokens,
      capabilities: {
        // Vision capability for image input
        imageInput: model.capabilities.vision,
        // Tool calling for agent/plan modes - enable for models that support it
        toolCalling: model.capabilities.tools,
      },
      tooltip: this.buildTooltip(model),
      detail: this.buildDetail(model),
    };
  }

  /**
   * Build tooltip string for model
   */
  private buildTooltip(model: ModelInfo): string {
    const caps: string[] = [];
    if (model.capabilities.code) caps.push('Code');
    if (model.capabilities.tools) caps.push('Tools');
    if (model.capabilities.vision) caps.push('Vision');

    const capStr = caps.length > 0 ? ` (${caps.join(', ')})` : '';
    return `${model.name}${capStr} - ${model.size}`;
  }

  /**
   * Build detail string for model
   */
  private buildDetail(model: ModelInfo): string {
    return `${model.size} - ${model.family}`;
  }

  /**
   * Handle chat requests and stream responses
   */
  async provideLanguageModelChatResponse(
    model: vscode.LanguageModelChatInformation,
    messages: readonly vscode.LanguageModelChatRequestMessage[],
    options: vscode.ProvideLanguageModelChatResponseOptions,
    progress: vscode.Progress<vscode.LanguageModelResponsePart>,
    token: vscode.CancellationToken
  ): Promise<void> {
    // Extract model name from ID (remove "ollama:" prefix)
    const modelName = model.id.replace('ollama:', '');

    // Convert VS Code messages to Ollama format
    const ollamaMessages = this.convertMessages(messages);

    // Handle tool calls if present
    const tools = options.tools?.map((tool) => ({
      type: 'function' as const,
      function: {
        name: tool.name,
        description: tool.description || '',
        parameters: tool.inputSchema || {},
      },
    }));

    return new Promise<void>((resolve, reject) => {
      // Set up cancellation
      token.onCancellationRequested(() => {
        ollamaClient.cancelGeneration();
      });

      // Stream the response
      ollamaClient
        .chatStream(
          {
            model: modelName,
            messages: ollamaMessages,
            options: {
              temperature: getConfiguration().temperature,
              num_predict: getConfiguration().maxTokens,
            },
            ...(tools && tools.length > 0 ? { tools } : {}),
          },
          (chunk: string, done: boolean) => {
            if (token.isCancellationRequested) {
              return;
            }

            // Report text chunks
            if (chunk) {
              progress.report(new vscode.LanguageModelTextPart(chunk));
            }

            if (done) {
              resolve();
            }
          }
        )
        .catch((error) => {
          if (token.isCancellationRequested) {
            resolve();
          } else {
            reject(error);
          }
        });
    });
  }

  /**
   * Convert VS Code chat messages to Ollama format
   * Note: VS Code only has User and Assistant roles, no System role
   */
  private convertMessages(
    messages: readonly vscode.LanguageModelChatRequestMessage[]
  ): Array<{ role: 'system' | 'user' | 'assistant'; content: string; images?: string[] }> {
    return messages.map((msg) => {
      let content = '';
      const images: string[] = [];

      // Handle different message part types
      for (const part of msg.content) {
        if (part instanceof vscode.LanguageModelTextPart) {
          content += part.value;
        } else if (part instanceof vscode.LanguageModelDataPart) {
          // Handle image data
          const base64 = Buffer.from(part.data).toString('base64');
          images.push(base64);
        }
      }

      // Map role - VS Code only has User and Assistant
      const role: 'user' | 'assistant' =
        msg.role === vscode.LanguageModelChatMessageRole.Assistant ? 'assistant' : 'user';

      return {
        role,
        content,
        ...(images.length > 0 ? { images } : {}),
      };
    });
  }

  /**
   * Provide token count estimation
   * Uses a simple approximation since Ollama doesn't expose tokenizer directly
   */
  async provideTokenCount(
    _model: vscode.LanguageModelChatInformation,
    text: string | vscode.LanguageModelChatRequestMessage,
    _token: vscode.CancellationToken
  ): Promise<number> {
    // Simple approximation: ~4 characters per token for English
    let totalChars = 0;

    if (typeof text === 'string') {
      totalChars = text.length;
    } else {
      // It's a LanguageModelChatRequestMessage
      for (const part of text.content) {
        if (part instanceof vscode.LanguageModelTextPart) {
          totalChars += part.value.length;
        }
      }
    }

    return Math.ceil(totalChars / 4);
  }
}

/**
 * Register the Ollama language model provider
 */
export function registerOllamaLanguageModelProvider(
  context: vscode.ExtensionContext
): vscode.Disposable {
  const provider = new OllamaLanguageModelProvider();
  const disposable = vscode.lm.registerLanguageModelChatProvider('ollama', provider);
  context.subscriptions.push(disposable);

  console.log('Ollama: Language Model Provider registered');
  return disposable;
}
