import * as vscode from 'vscode';
import axios, { AxiosInstance } from 'axios';
import { EventEmitter } from 'events';
import { randomUUID } from 'crypto';
import { RetryBackoffHandle, scheduleRetryWithBackoff } from './retryBackoff';

/** A single OpenAI-compatible chat message. */
export interface ChatMessage {
    role: 'system' | 'user' | 'assistant';
    content: string;
}

/**
 * Additive `usage.waddleai` block the proxy attaches to `/v1/chat/completions`
 * responses (spec §6.4 cache, §6A.5 proxy-memory, §7.6 routing). Every field
 * is optional and independently omitted when its feature is flag-off or had
 * no effect on this particular request -- see
 * `proxy/apps/proxy_server/main.py::_merge_waddleai_usage`.
 */
export interface WaddleAIUsageMetadata {
    cache?: 'exact' | 'semantic' | 'upstream';
    cached_tokens?: number;
    tokens_saved?: number;
    summarized?: boolean;
    tokens_elided?: number;
    injected_tokens?: number;
    /** Present only when RoutingStage redirected the requested model; shape varies by cause. */
    routed_from?: Record<string, unknown>;
}

export interface WaddleAIChatUsage {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    waddleai_tokens: number;
    waddleai?: WaddleAIUsageMetadata;
}

export interface WaddleAIChatChoice {
    index: number;
    message: { role: string; content: string };
    finish_reason: string;
}

export interface WaddleAIChatCompletionResponse {
    id: string;
    object: string;
    created: number;
    model: string;
    choices: WaddleAIChatChoice[];
    usage: WaddleAIChatUsage;
}

export interface WaddleAIModel {
    id: string;
    [key: string]: unknown;
}

/**
 * Typed outcome of `getAvailableModels()` -- distinguishes a legitimately
 * empty model list from a failure that prevented the client from finding
 * out (O8 Low: the old code logged-and-swallowed every error behind a bare
 * `[]`, which looked identical to "the server has no models" to every
 * caller). `reason: 'auth'` is a confirmed 401/403 and is never retried;
 * `reason: 'network'` covers everything else (timeout, DNS, 5xx) and is
 * retried with backoff.
 */
export type ModelFetchResult =
    | { ok: true; models: WaddleAIModel[] }
    | { ok: false; reason: 'auth' | 'network'; error: Error };

interface DailyQuota {
    used: number;
    limit: number;
    remaining: number;
    ok: boolean;
}

interface ModelTokenBreakdown {
    input: number;
    output: number;
}

/** Combined view over `/api/usage` (rolling 30-day stats) + `/api/quota` (daily/monthly limits). */
export interface WaddleAIUsageSummary {
    total_waddleai_tokens: number;
    total_llm_input_tokens: number;
    total_llm_output_tokens: number;
    total_requests: number;
    average_daily: number;
    llm_breakdown: Record<string, ModelTokenBreakdown>;
    daily: DailyQuota;
    monthly: DailyQuota;
}

/**
 * WaddleAI API Client
 * Handles all communication with the WaddleAI proxy server, pointed at the
 * routes the proxy actually serves today (`/v1/models`, `/v1/chat/completions`,
 * `/api/usage`, `/api/quota`, `/readyz`) -- see docs/integrations/vscode-extension.md
 * for the mapping this client is refreshed against.
 */
export class WaddleAIClient extends EventEmitter {
    private axiosInstance: AxiosInstance;
    private apiKey: string | undefined;
    private endpoint: string;
    private sessionId: string;
    // Suppresses repeat showWarningMessage/showErrorMessage popups for
    // consecutive getAvailableModels() failures -- reset on the next
    // success so a *new* outage still gets its own notification.
    private modelFetchOutageNotified = false;
    private modelFetchRetryHandle: RetryBackoffHandle | undefined;

    constructor(private context: vscode.ExtensionContext) {
        super();

        const config = vscode.workspace.getConfiguration('waddleai');
        this.endpoint = config.get<string>('apiEndpoint') || 'http://localhost:8000';
        this.sessionId = this.generateSessionId();

        // Initialize axios instance
        this.axiosInstance = axios.create({
            baseURL: this.endpoint,
            timeout: 30000,
            headers: {
                'Content-Type': 'application/json',
                'X-WaddleAI-Session': this.sessionId,
                'X-Client': 'vscode-extension',
                'X-Client-Version': this.getExtensionVersion()
            }
        });

        // Load API key from secure storage or config
        this.loadApiKey();

        // Set up request interceptor for authentication
        this.axiosInstance.interceptors.request.use(
            (config) => {
                if (this.apiKey) {
                    config.headers['Authorization'] = `Bearer ${this.apiKey}`;
                }
                return config;
            },
            (error) => {
                return Promise.reject(error);
            }
        );

        // Set up response interceptor for error handling
        this.axiosInstance.interceptors.response.use(
            (response) => response,
            async (error) => {
                if (error.response?.status === 401) {
                    // Token might be expired, try to refresh
                    await this.handleAuthError();
                }
                return Promise.reject(error);
            }
        );
    }

    private async loadApiKey() {
        // Try to load from secure storage first
        this.apiKey = await this.context.secrets.get('waddleai.apiKey');

        // Fallback to configuration
        if (!this.apiKey) {
            const config = vscode.workspace.getConfiguration('waddleai');
            this.apiKey = config.get<string>('apiKey');
        }
    }

    /**
     * Mint a session id for the `X-WaddleAI-Session` header.
     *
     * Uses crypto.randomUUID(), not Math.random(). This id is not cosmetic:
     * the server scopes conversation memory by it (spec 6A), so a guessable
     * id would let one client read or pollute another's memory scope.
     * Math.random() is a non-cryptographic PRNG whose future output is
     * recoverable from a handful of prior values -- and the previous format
     * leaked a second predictor by prefixing Date.now().
     */
    private generateSessionId(): string {
        return `vscode-${randomUUID()}`;
    }

    private getExtensionVersion(): string {
        const packageJson = this.context.extension.packageJSON;
        return packageJson.version || '0.0.0';
    }

    private async handleAuthError() {
        const action = await vscode.window.showErrorMessage(
            'WaddleAI authentication failed. Please update your API key.',
            'Update API Key',
            'Cancel'
        );

        if (action === 'Update API Key') {
            await vscode.commands.executeCommand('waddleai.setApiKey');
            await this.loadApiKey();
        }
    }

    /**
     * Test connection to the WaddleAI proxy via its Kubernetes-style
     * readiness probe (`/readyz`) -- gates on the proxy's hard local
     * dependency (its database), unlike `/healthz` which is a bare
     * liveness check with no dependency signal.
     */
    async testConnection(): Promise<{ ready: boolean; details?: unknown }> {
        try {
            const response = await this.axiosInstance.get('/readyz');
            return { ready: response.status === 200, details: response.data };
        } catch (error: any) {
            if (error.response) {
                return { ready: false, details: error.response.data };
            }
            throw new Error(`Connection failed: ${error.message}`);
        }
    }

    /**
     * Get available models from WaddleAI. Never throws and never swallows
     * an error into an indistinguishable empty array -- the return value
     * tells the caller exactly what happened, and a network failure also
     * triggers a throttled user-facing warning + background retry (see
     * `notifyModelFetchFailure`).
     */
    async getAvailableModels(): Promise<ModelFetchResult> {
        try {
            const response = await this.axiosInstance.get('/v1/models');
            const models: WaddleAIModel[] = response.data?.data || [];
            this.notifyModelFetchSucceeded();
            return { ok: true, models };
        } catch (error: any) {
            const result = this.classifyModelFetchError(error);
            this.notifyModelFetchFailure(result);
            return result;
        }
    }

    /** Maps a caught error to the typed, non-throwing `ModelFetchResult` failure shape. */
    private classifyModelFetchError(error: any): { ok: false; reason: 'auth' | 'network'; error: Error } {
        const status = error?.response?.status;
        const reason: 'auth' | 'network' = status === 401 || status === 403 ? 'auth' : 'network';
        const normalized = error instanceof Error ? error : new Error(String(error?.message ?? error));
        return { ok: false, reason, error: normalized };
    }

    /** A successful fetch ends any outage: re-arm the warning and stop any background retry. */
    private notifyModelFetchSucceeded(): void {
        this.modelFetchOutageNotified = false;
        this.stopModelFetchRetry();
    }

    /**
     * Surfaces a getAvailableModels() failure to the user exactly once per
     * outage window, and only ever schedules a background retry for
     * `reason: 'network'` -- a 401/403 is a confirmed auth decision, never
     * retried, per O8 Low (`vscode-extension/.../waddleaiClient.ts:204-212`).
     */
    private notifyModelFetchFailure(result: { ok: false; reason: 'auth' | 'network'; error: Error }): void {
        console.error('[WaddleAIClient] getAvailableModels failed', {
            reason: result.reason,
            message: result.error.message
        });

        if (result.reason === 'auth') {
            this.stopModelFetchRetry();
            if (!this.modelFetchOutageNotified) {
                this.modelFetchOutageNotified = true;
                void this.handleAuthError();
            }
            return;
        }

        if (!this.modelFetchOutageNotified) {
            this.modelFetchOutageNotified = true;
            void vscode.window
                .showWarningMessage(
                    'WaddleAI: unable to reach the server to list models. Check your connection or the proxy endpoint.',
                    'Retry'
                )
                .then((action) => {
                    if (action === 'Retry') {
                        void this.getAvailableModels();
                    }
                });
        }

        if (!this.modelFetchRetryHandle) {
            this.modelFetchRetryHandle = scheduleRetryWithBackoff(() => {
                void this.getAvailableModels();
            });
        }
    }

    private stopModelFetchRetry(): void {
        if (this.modelFetchRetryHandle) {
            this.modelFetchRetryHandle.stop();
            this.modelFetchRetryHandle = undefined;
        }
    }

    /**
     * Chat completion via the OpenAI-compatible `/v1/chat/completions`
     * endpoint. The proxy always returns one JSON envelope regardless of
     * a `stream` flag in the request body (there is no `text/event-stream`
     * response path today), so this client requests `stream: false`
     * explicitly rather than parsing a Server-Sent-Events response that
     * the server never sends.
     */
    async chatCompletion(
        messages: ChatMessage[],
        model: string,
        options: Record<string, unknown> = {}
    ): Promise<WaddleAIChatCompletionResponse> {
        const requestBody = {
            model,
            messages,
            stream: false,
            ...options
        };

        try {
            const response = await this.axiosInstance.post<WaddleAIChatCompletionResponse>(
                '/v1/chat/completions',
                requestBody
            );
            return response.data;
        } catch (error: any) {
            throw new Error(`Chat completion failed: ${error.message}`);
        }
    }

    /**
     * Combined usage view: `/api/usage` (rolling 30-day token/request
     * totals, server-side fixed window -- it does not accept a `days`
     * query param) merged with `/api/quota` (current daily/monthly limits
     * and remaining balance for this API key).
     */
    async getUsage(): Promise<WaddleAIUsageSummary> {
        try {
            const [usageRes, quotaRes] = await Promise.all([
                this.axiosInstance.get('/api/usage'),
                this.axiosInstance.get('/api/quota')
            ]);
            const usage = usageRes.data || {};
            const quota = quotaRes.data || {};
            return {
                total_waddleai_tokens: usage.total_waddleai_tokens || 0,
                total_llm_input_tokens: usage.total_llm_input_tokens || 0,
                total_llm_output_tokens: usage.total_llm_output_tokens || 0,
                total_requests: usage.total_requests || 0,
                average_daily: usage.average_daily || 0,
                llm_breakdown: usage.llm_breakdown || {},
                daily: quota.daily || { used: 0, limit: 0, remaining: 0, ok: true },
                monthly: quota.monthly || { used: 0, limit: 0, remaining: 0, ok: true }
            };
        } catch (error: any) {
            throw new Error(`Failed to fetch usage: ${error.message}`);
        }
    }

    /**
     * Start a new conversation scope. WaddleAI's proxy-memory layers key
     * off the `X-WaddleAI-Session` header (spec §6A) -- there is no
     * server-side "delete this session's memory" route today, so
     * resetting the client's own session id is what actually starts a
     * fresh memory scope for the next request, rather than calling a
     * route that doesn't exist.
     */
    resetSession(): string {
        this.sessionId = this.generateSessionId();
        this.axiosInstance.defaults.headers['X-WaddleAI-Session'] = this.sessionId;
        return this.sessionId;
    }

    /**
     * Update configuration
     */
    async updateConfiguration(newEndpoint?: string, newApiKey?: string) {
        if (newEndpoint && newEndpoint !== this.endpoint) {
            this.endpoint = newEndpoint;
            this.axiosInstance.defaults.baseURL = newEndpoint;
        }

        if (newApiKey && newApiKey !== this.apiKey) {
            this.apiKey = newApiKey;
            await this.context.secrets.store('waddleai.apiKey', newApiKey);
        }
    }

    /**
     * Dispose of resources
     */
    dispose() {
        this.removeAllListeners();
        this.stopModelFetchRetry();
    }
}
