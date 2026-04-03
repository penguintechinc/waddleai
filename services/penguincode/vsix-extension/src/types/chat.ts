/**
 * Chat Types for Webview Communication
 */

import { ModelInfo } from './ollama';

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: number;
  model?: string;
  isStreaming?: boolean;
  error?: string;
}

export interface ChatState {
  messages: ChatMessage[];
  currentModel: string;
  availableModels: ModelInfo[];
  isLoading: boolean;
  isConnected: boolean;
  error?: string;
}

export type WebviewMessageType =
  | 'sendMessage'
  | 'selectModel'
  | 'clearHistory'
  | 'cancelGeneration'
  | 'refreshModels'
  | 'getState'
  | 'copyCode'
  | 'insertCode';

export interface WebviewMessage {
  type: WebviewMessageType;
  payload?: unknown;
  content?: string;
  model?: string;
  code?: string;
}

export type ExtensionMessageType =
  | 'streamChunk'
  | 'streamEnd'
  | 'stateUpdate'
  | 'error'
  | 'modelsLoaded'
  | 'connectionStatus';

export interface ExtensionMessage {
  type: ExtensionMessageType;
  payload: unknown;
}

export interface StreamChunkPayload {
  id: string;
  content: string;
  done: boolean;
}

export interface ModelsLoadedPayload {
  models: ModelInfo[];
  current: string;
}

export interface ConnectionStatusPayload {
  connected: boolean;
}

export interface ErrorPayload {
  message: string;
}
