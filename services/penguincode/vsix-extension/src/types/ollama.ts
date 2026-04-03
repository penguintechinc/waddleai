/**
 * Ollama API Types
 * Based on Ollama API documentation: https://github.com/ollama/ollama/blob/main/docs/api.md
 */

export interface OllamaModel {
  name: string;
  model: string;
  modified_at: string;
  size: number;
  digest: string;
  details: OllamaModelDetails;
}

export interface OllamaModelDetails {
  parent_model: string;
  format: string;
  family: string;
  families: string[];
  parameter_size: string;
  quantization_level: string;
}

export interface OllamaTagsResponse {
  models: OllamaModel[];
}

export interface OllamaOptions {
  num_keep?: number;
  seed?: number;
  num_predict?: number;
  top_k?: number;
  top_p?: number;
  tfs_z?: number;
  typical_p?: number;
  repeat_last_n?: number;
  temperature?: number;
  repeat_penalty?: number;
  presence_penalty?: number;
  frequency_penalty?: number;
  mirostat?: number;
  mirostat_tau?: number;
  mirostat_eta?: number;
  penalize_newline?: boolean;
  stop?: string[];
  numa?: boolean;
  num_ctx?: number;
  num_batch?: number;
  num_gpu?: number;
  main_gpu?: number;
  low_vram?: boolean;
  vocab_only?: boolean;
  use_mmap?: boolean;
  use_mlock?: boolean;
  num_thread?: number;
}

export interface OllamaGenerateRequest {
  model: string;
  prompt: string;
  suffix?: string;
  images?: string[];
  format?: 'json' | object;
  options?: OllamaOptions;
  system?: string;
  template?: string;
  context?: number[];
  stream?: boolean;
  raw?: boolean;
  keep_alive?: string;
}

export interface OllamaGenerateResponse {
  model: string;
  created_at: string;
  response: string;
  done: boolean;
  done_reason?: string;
  context?: number[];
  total_duration?: number;
  load_duration?: number;
  prompt_eval_count?: number;
  prompt_eval_duration?: number;
  eval_count?: number;
  eval_duration?: number;
}

export interface OllamaChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
  images?: string[];
}

export interface OllamaChatRequest {
  model: string;
  messages: OllamaChatMessage[];
  format?: 'json' | object;
  options?: OllamaOptions;
  stream?: boolean;
  keep_alive?: string;
}

export interface OllamaChatResponse {
  model: string;
  created_at: string;
  message: OllamaChatMessage;
  done: boolean;
  done_reason?: string;
  total_duration?: number;
  load_duration?: number;
  prompt_eval_count?: number;
  prompt_eval_duration?: number;
  eval_count?: number;
  eval_duration?: number;
}

export interface OllamaError {
  error: string;
}

/**
 * Callback for streaming responses
 * @param chunk - The text chunk received
 * @param done - Whether this is the final chunk
 */
export type OllamaStreamCallback = (chunk: string, done: boolean) => void;

/**
 * Model capabilities
 */
export interface ModelCapabilities {
  vision: boolean;
  tools: boolean;
  code: boolean;
}

/**
 * Model info with capabilities
 */
export interface ModelInfo {
  name: string;
  size: string;
  family: string;
  capabilities: ModelCapabilities;
}
