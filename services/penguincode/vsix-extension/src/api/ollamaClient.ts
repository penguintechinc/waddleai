/**
 * Ollama API Client
 * Handles communication with the Ollama API server with streaming support
 */

import {
  OllamaGenerateRequest,
  OllamaGenerateResponse,
  OllamaChatRequest,
  OllamaChatResponse,
  OllamaChatMessage,
  OllamaTagsResponse,
  OllamaStreamCallback,
  ModelInfo,
  ModelCapabilities,
} from '../types/ollama';
import { getConfiguration } from '../config/configuration';

/**
 * Client for interacting with the Ollama API
 */
class OllamaClient {
  private abortController: AbortController | null = null;

  /**
   * Get the API URL from configuration
   */
  private getApiUrl(): string {
    const config = getConfiguration();
    return config.apiUrl;
  }

  /**
   * Fetch with timeout support
   */
  private async fetchWithTimeout(
    endpoint: string,
    options: RequestInit = {},
    timeoutMs?: number
  ): Promise<Response> {
    const config = getConfiguration();
    const timeout = timeoutMs ?? config.timeout;
    const url = `${this.getApiUrl()}${endpoint}`;

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeout);

    try {
      const response = await fetch(url, {
        ...options,
        signal: controller.signal,
      });
      clearTimeout(timeoutId);
      return response;
    } catch (error) {
      clearTimeout(timeoutId);
      throw error;
    }
  }

  /**
   * Handle streaming response (NDJSON format)
   */
  private async handleStream<T extends object>(
    response: Response,
    callback: OllamaStreamCallback
  ): Promise<void> {
    if (!response.body) {
      throw new Error('Response body is null');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    try {
      while (true) {
        const { done, value } = await reader.read();

        if (done) {
          break;
        }

        // Decode the chunk and add to buffer
        buffer += decoder.decode(value, { stream: true });

        // Process complete lines (NDJSON format)
        const lines = buffer.split('\n');

        // Keep the last incomplete line in the buffer
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.trim()) {
            try {
              const data = JSON.parse(line) as T;

              // Handle different response types
              if ('response' in data && typeof (data as any).response === 'string') {
                // OllamaGenerateResponse
                const generateResponse = data as unknown as OllamaGenerateResponse;
                callback(generateResponse.response, generateResponse.done ?? false);
              } else if ('message' in data && (data as any).message) {
                // OllamaChatResponse
                const chatResponse = data as unknown as OllamaChatResponse;
                callback(chatResponse.message.content, chatResponse.done ?? false);
              }
            } catch (parseError) {
              console.error('Failed to parse JSON line:', line, parseError);
            }
          }
        }
      }

      // Process any remaining data in the buffer
      if (buffer.trim()) {
        try {
          const data = JSON.parse(buffer) as T;
          if ('response' in data && typeof (data as any).response === 'string') {
            const generateResponse = data as unknown as OllamaGenerateResponse;
            callback(generateResponse.response, generateResponse.done ?? false);
          } else if ('message' in data && (data as any).message) {
            const chatResponse = data as unknown as OllamaChatResponse;
            callback(chatResponse.message.content, chatResponse.done ?? false);
          }
        } catch (parseError) {
          console.error('Failed to parse final JSON:', buffer, parseError);
        }
      }
    } finally {
      reader.releaseLock();
    }
  }

  /**
   * Check if Ollama server is available
   */
  async isAvailable(): Promise<boolean> {
    try {
      const response = await this.fetchWithTimeout('/api/tags', {}, 5000);
      return response.ok;
    } catch (error) {
      return false;
    }
  }

  /**
   * Alias for isAvailable() - check connection to Ollama server
   */
  async checkConnection(): Promise<boolean> {
    return this.isAvailable();
  }

  /**
   * List available models
   */
  async listModels(): Promise<string[]> {
    try {
      const response = await this.fetchWithTimeout('/api/tags');

      if (!response.ok) {
        throw new Error(`Failed to fetch models: ${response.statusText}`);
      }

      const data = await response.json() as OllamaTagsResponse;
      return data.models.map((model) => model.name);
    } catch (error) {
      console.error('Error listing models:', error);
      throw error;
    }
  }

  /**
   * List models with detailed info and capabilities
   */
  async listModelsWithInfo(): Promise<ModelInfo[]> {
    try {
      const response = await this.fetchWithTimeout('/api/tags');

      if (!response.ok) {
        throw new Error(`Failed to fetch models: ${response.statusText}`);
      }

      const data = await response.json() as OllamaTagsResponse;
      return data.models.map((model) => ({
        name: model.name,
        size: model.details?.parameter_size || 'Unknown',
        family: model.details?.family || 'unknown',
        capabilities: this.detectCapabilities(model.name, model.details?.family || ''),
      }));
    } catch (error) {
      console.error('Error listing models:', error);
      throw error;
    }
  }

  /**
   * Detect model capabilities based on name and family
   */
  private detectCapabilities(name: string, family: string): ModelCapabilities {
    const nameLower = name.toLowerCase();
    const familyLower = family.toLowerCase();

    // Vision models
    const visionPatterns = [
      'llava', 'vision', 'llama3.2-vision', 'bakllava', 'moondream',
      'cogvlm', 'yi-vl', 'internvl', 'minicpm-v'
    ];
    const hasVision = visionPatterns.some(p => nameLower.includes(p));

    // Tool/Function calling models
    const toolPatterns = [
      'llama3.1', 'llama3.2', 'llama3.3', 'mistral', 'mixtral',
      'qwen2', 'qwen2.5', 'command-r', 'firefunction', 'hermes',
      'nexusraven', 'functionary'
    ];
    const hasTools = toolPatterns.some(p => nameLower.includes(p));

    // Code-specialized models
    const codePatterns = [
      'codellama', 'code', 'deepseek-coder', 'starcoder', 'wizardcoder',
      'phind', 'codegemma', 'codestral', 'codeqwen', 'qwen2.5-coder',
      'granite-code', 'stable-code', 'opencoder', 'yi-coder'
    ];
    const hasCode = codePatterns.some(p => nameLower.includes(p)) ||
                   familyLower === 'codellama';

    return {
      vision: hasVision,
      tools: hasTools,
      code: hasCode,
    };
  }

  /**
   * Generate completion (non-streaming)
   */
  async generate(request: OllamaGenerateRequest): Promise<string> {
    try {
      const response = await this.fetchWithTimeout('/api/generate', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          ...request,
          stream: false,
        }),
      });

      if (!response.ok) {
        throw new Error(`Generation failed: ${response.statusText}`);
      }

      const data = await response.json() as OllamaGenerateResponse;
      return data.response;
    } catch (error) {
      console.error('Error generating completion:', error);
      throw error;
    }
  }

  /**
   * Chat completion (non-streaming)
   */
  async chat(request: OllamaChatRequest): Promise<OllamaChatMessage> {
    try {
      const response = await this.fetchWithTimeout('/api/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          ...request,
          stream: false,
        }),
      });

      if (!response.ok) {
        throw new Error(`Chat failed: ${response.statusText}`);
      }

      const data = await response.json() as OllamaChatResponse;
      return data.message;
    } catch (error) {
      console.error('Error in chat completion:', error);
      throw error;
    }
  }

  /**
   * Generate completion with streaming
   */
  async generateStream(
    request: OllamaGenerateRequest,
    callback: OllamaStreamCallback
  ): Promise<void> {
    this.abortController = new AbortController();

    try {
      const response = await fetch(`${this.getApiUrl()}/api/generate`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          ...request,
          stream: true,
        }),
        signal: this.abortController.signal,
      });

      if (!response.ok) {
        throw new Error(`Stream generation failed: ${response.statusText}`);
      }

      await this.handleStream<OllamaGenerateResponse>(response, callback);
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') {
        console.log('Generation cancelled');
      } else {
        console.error('Error in stream generation:', error);
        throw error;
      }
    } finally {
      this.abortController = null;
    }
  }

  /**
   * Chat completion with streaming
   */
  async chatStream(
    request: OllamaChatRequest,
    callback: OllamaStreamCallback
  ): Promise<void> {
    this.abortController = new AbortController();

    try {
      const response = await fetch(`${this.getApiUrl()}/api/chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          ...request,
          stream: true,
        }),
        signal: this.abortController.signal,
      });

      if (!response.ok) {
        throw new Error(`Stream chat failed: ${response.statusText}`);
      }

      await this.handleStream<OllamaChatResponse>(response, callback);
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') {
        console.log('Chat cancelled');
      } else {
        console.error('Error in stream chat:', error);
        throw error;
      }
    } finally {
      this.abortController = null;
    }
  }

  /**
   * Cancel ongoing generation/chat request
   */
  cancelGeneration(): void {
    if (this.abortController) {
      this.abortController.abort();
      this.abortController = null;
    }
  }
}

// Export singleton instance
export const ollamaClient = new OllamaClient();
