import * as vscode from 'vscode';

// Single shared fake axios instance returned by every axios.create() call in
// these tests -- WaddleAIClient never touches the real network.
const mockAxiosInstance = {
    get: jest.fn(),
    post: jest.fn(),
    defaults: { headers: {} as Record<string, string>, baseURL: '' },
    interceptors: {
        request: { use: jest.fn() },
        response: { use: jest.fn() }
    }
};

jest.mock('axios', () => ({
    __esModule: true,
    default: {
        create: jest.fn(() => mockAxiosInstance)
    }
}));

import axios from 'axios';
import { WaddleAIClient, ModelFetchResult } from '../waddleaiClient';

function makeContext(overrides: Partial<vscode.ExtensionContext> = {}): vscode.ExtensionContext {
    return {
        secrets: {
            get: jest.fn().mockResolvedValue(undefined),
            store: jest.fn().mockResolvedValue(undefined)
        },
        extension: { packageJSON: { version: '0.2.0-test' } },
        ...overrides
    } as unknown as vscode.ExtensionContext;
}

function httpError(status: number, message = 'request failed'): any {
    const error: any = new Error(message);
    error.response = { status, data: {} };
    return error;
}

function networkError(message = 'ECONNREFUSED'): any {
    return new Error(message);
}

describe('WaddleAIClient construction', () => {
    beforeEach(() => {
        jest.clearAllMocks();
        (vscode.workspace.getConfiguration as jest.Mock).mockReturnValue({ get: jest.fn() });
    });

    it('falls back to "0.0.0" for X-Client-Version when the extension has no package version', () => {
        const ctx = {
            secrets: { get: jest.fn().mockResolvedValue(undefined), store: jest.fn() },
            extension: { packageJSON: {} }
        } as unknown as vscode.ExtensionContext;

        const localClient = new WaddleAIClient(ctx);

        expect(axios.create).toHaveBeenCalledWith(
            expect.objectContaining({ headers: expect.objectContaining({ 'X-Client-Version': '0.0.0' }) })
        );
        localClient.dispose();
    });
});

describe('WaddleAIClient', () => {
    let client: WaddleAIClient;
    let context: vscode.ExtensionContext;

    beforeEach(() => {
        jest.clearAllMocks();
        mockAxiosInstance.defaults.headers = {};
        mockAxiosInstance.defaults.baseURL = '';
        (vscode.workspace.getConfiguration as jest.Mock).mockReturnValue({
            get: jest.fn((key: string) => (key === 'apiEndpoint' ? 'http://localhost:9000' : undefined))
        });
        context = makeContext();
        client = new WaddleAIClient(context);
    });

    afterEach(() => {
        client.dispose();
        jest.useRealTimers();
    });

    describe('getAvailableModels()', () => {
        it('returns ok:true with the model list on success and clears any prior outage flag', async () => {
            mockAxiosInstance.get.mockResolvedValueOnce({ data: { data: [{ id: 'gpt-4' }, { id: 'claude-3' }] } });

            const result = await client.getAvailableModels();

            expect(result).toEqual<ModelFetchResult>({ ok: true, models: [{ id: 'gpt-4' }, { id: 'claude-3' }] });
            expect(vscode.window.showWarningMessage).not.toHaveBeenCalled();
        });

        it('defaults to an empty model array (not a crash) when the response has no data.data', async () => {
            mockAxiosInstance.get.mockResolvedValueOnce({ data: {} });

            const result = await client.getAvailableModels();

            expect(result).toEqual<ModelFetchResult>({ ok: true, models: [] });
        });

        it('classifies a 401 as reason: "auth", never shows the network warning, and prompts re-auth once', async () => {
            (vscode.window.showErrorMessage as jest.Mock).mockResolvedValue(undefined);
            mockAxiosInstance.get.mockRejectedValueOnce(httpError(401));

            const result = await client.getAvailableModels();

            expect(result.ok).toBe(false);
            if (!result.ok) {
                expect(result.reason).toBe('auth');
            }
            expect(vscode.window.showWarningMessage).not.toHaveBeenCalled();
            expect(vscode.window.showErrorMessage).toHaveBeenCalledTimes(1);
        });

        it('classifies a 403 as reason: "auth" too', async () => {
            (vscode.window.showErrorMessage as jest.Mock).mockResolvedValue(undefined);
            mockAxiosInstance.get.mockRejectedValueOnce(httpError(403));

            const result = await client.getAvailableModels();

            expect(result.ok).toBe(false);
            if (!result.ok) {
                expect(result.reason).toBe('auth');
            }
        });

        it('classifies a thrown network error (no response) as reason: "network" and warns once', async () => {
            (vscode.window.showWarningMessage as jest.Mock).mockResolvedValue(undefined);
            mockAxiosInstance.get.mockRejectedValueOnce(networkError());

            const result = await client.getAvailableModels();

            expect(result.ok).toBe(false);
            if (!result.ok) {
                expect(result.reason).toBe('network');
            }
            expect(vscode.window.showWarningMessage).toHaveBeenCalledTimes(1);
            const [, action] = (vscode.window.showWarningMessage as jest.Mock).mock.calls[0];
            expect(action).toBe('Retry');
        });

        it('classifies a 500 as reason: "network" (only 401/403 are auth decisions)', async () => {
            (vscode.window.showWarningMessage as jest.Mock).mockResolvedValue(undefined);
            mockAxiosInstance.get.mockRejectedValueOnce(httpError(500));

            const result = await client.getAvailableModels();

            expect(result.ok).toBe(false);
            if (!result.ok) {
                expect(result.reason).toBe('network');
            }
        });

        it('does NOT re-show the network warning on a second consecutive network failure (once per outage window)', async () => {
            (vscode.window.showWarningMessage as jest.Mock).mockResolvedValue(undefined);
            mockAxiosInstance.get.mockRejectedValueOnce(networkError()).mockRejectedValueOnce(networkError());

            await client.getAvailableModels();
            await client.getAvailableModels();

            expect(vscode.window.showWarningMessage).toHaveBeenCalledTimes(1);
        });

        it('shows the warning again on a NEW outage after a recovery in between', async () => {
            (vscode.window.showWarningMessage as jest.Mock).mockResolvedValue(undefined);
            mockAxiosInstance.get
                .mockRejectedValueOnce(networkError())
                .mockResolvedValueOnce({ data: { data: [] } })
                .mockRejectedValueOnce(networkError());

            await client.getAvailableModels(); // outage #1 -> warns
            await client.getAvailableModels(); // recovers -> clears the flag
            await client.getAvailableModels(); // outage #2 -> warns again

            expect(vscode.window.showWarningMessage).toHaveBeenCalledTimes(2);
        });

        it('does NOT re-show the auth prompt on a second consecutive 401 (once per outage window)', async () => {
            (vscode.window.showErrorMessage as jest.Mock).mockResolvedValue(undefined);
            mockAxiosInstance.get.mockRejectedValueOnce(httpError(401)).mockRejectedValueOnce(httpError(401));

            await client.getAvailableModels();
            await client.getAvailableModels();

            expect(vscode.window.showErrorMessage).toHaveBeenCalledTimes(1);
        });

        it('clicking "Retry" on the network warning re-invokes getAvailableModels()', async () => {
            (vscode.window.showWarningMessage as jest.Mock).mockResolvedValueOnce('Retry');
            mockAxiosInstance.get
                .mockRejectedValueOnce(networkError())
                .mockResolvedValueOnce({ data: { data: [{ id: 'gpt-4' }] } });

            await client.getAvailableModels();
            // the warning's .then() runs on microtask resolution
            await new Promise((resolve) => setImmediate(resolve));

            expect(mockAxiosInstance.get).toHaveBeenCalledTimes(2);
        });

        it('schedules a background retry on a network failure, which stops once it succeeds', async () => {
            jest.useFakeTimers();
            (vscode.window.showWarningMessage as jest.Mock).mockResolvedValue(undefined);
            mockAxiosInstance.get
                .mockRejectedValueOnce(networkError())
                .mockResolvedValueOnce({ data: { data: [{ id: 'gpt-4' }] } });

            await client.getAvailableModels();
            expect(mockAxiosInstance.get).toHaveBeenCalledTimes(1);

            await jest.advanceTimersByTimeAsync(2000); // first backoff attempt (base delay)
            expect(mockAxiosInstance.get).toHaveBeenCalledTimes(2);

            // No further attempts scheduled once the retry succeeded.
            await jest.advanceTimersByTimeAsync(60000);
            expect(mockAxiosInstance.get).toHaveBeenCalledTimes(2);
        });

        it('never schedules a background retry for an auth (401/403) failure', async () => {
            jest.useFakeTimers();
            (vscode.window.showErrorMessage as jest.Mock).mockResolvedValue(undefined);
            mockAxiosInstance.get.mockRejectedValueOnce(httpError(401));

            await client.getAvailableModels();
            expect(mockAxiosInstance.get).toHaveBeenCalledTimes(1);

            await jest.advanceTimersByTimeAsync(60000);
            expect(mockAxiosInstance.get).toHaveBeenCalledTimes(1);
        });

        it('wraps a non-Error rejection in a normalized Error instead of crashing', async () => {
            (vscode.window.showWarningMessage as jest.Mock).mockResolvedValue(undefined);
            mockAxiosInstance.get.mockRejectedValueOnce('boom');

            const result = await client.getAvailableModels();

            expect(result.ok).toBe(false);
            if (!result.ok) {
                expect(result.error).toBeInstanceOf(Error);
            }
        });
    });

    describe('testConnection()', () => {
        it('returns ready:true on a 200 /readyz response', async () => {
            mockAxiosInstance.get.mockResolvedValueOnce({ status: 200, data: { db: 'ok' } });

            const result = await client.testConnection();

            expect(result).toEqual({ ready: true, details: { db: 'ok' } });
        });

        it('returns ready:false with error response details when the server responds non-200', async () => {
            mockAxiosInstance.get.mockRejectedValueOnce(httpError(503, 'not ready'));

            const result = await client.testConnection();

            expect(result.ready).toBe(false);
        });

        it('throws a wrapped error when there is no HTTP response at all (network failure)', async () => {
            mockAxiosInstance.get.mockRejectedValueOnce(networkError('timeout'));

            await expect(client.testConnection()).rejects.toThrow('Connection failed: timeout');
        });
    });

    describe('chatCompletion()', () => {
        it('posts the request body with stream:false and returns the response data', async () => {
            const responseData = { id: 'x', object: 'chat.completion', created: 1, model: 'gpt-4', choices: [], usage: {} };
            mockAxiosInstance.post.mockResolvedValueOnce({ data: responseData });

            const result = await client.chatCompletion([{ role: 'user', content: 'hi' }], 'gpt-4');

            expect(mockAxiosInstance.post).toHaveBeenCalledWith(
                '/v1/chat/completions',
                expect.objectContaining({ model: 'gpt-4', stream: false })
            );
            expect(result).toEqual(responseData);
        });

        it('wraps a failure in a descriptive Error', async () => {
            mockAxiosInstance.post.mockRejectedValueOnce(new Error('upstream down'));

            await expect(client.chatCompletion([], 'gpt-4')).rejects.toThrow('Chat completion failed: upstream down');
        });
    });

    describe('getUsage()', () => {
        it('merges /api/usage and /api/quota with safe defaults', async () => {
            mockAxiosInstance.get.mockImplementation((path: string) => {
                if (path === '/api/usage') {
                    return Promise.resolve({ data: { total_waddleai_tokens: 10 } });
                }
                return Promise.resolve({ data: {} });
            });

            const usage = await client.getUsage();

            expect(usage.total_waddleai_tokens).toBe(10);
            expect(usage.daily).toEqual({ used: 0, limit: 0, remaining: 0, ok: true });
        });

        it('wraps a failure in a descriptive Error', async () => {
            mockAxiosInstance.get.mockRejectedValue(new Error('db down'));

            await expect(client.getUsage()).rejects.toThrow('Failed to fetch usage: db down');
        });
    });

    describe('resetSession()', () => {
        it('mints a new vscode-prefixed session id and updates the default header', () => {
            const id = client.resetSession();

            expect(id).toMatch(/^vscode-/);
            expect(mockAxiosInstance.defaults.headers['X-WaddleAI-Session']).toBe(id);
        });
    });

    describe('updateConfiguration()', () => {
        it('updates the baseURL when a new endpoint is given', async () => {
            await client.updateConfiguration('http://new-host:8000');

            expect(mockAxiosInstance.defaults.baseURL).toBe('http://new-host:8000');
        });

        it('stores a new API key in secret storage when given', async () => {
            const context = makeContext();
            const localClient = new WaddleAIClient(context);

            await localClient.updateConfiguration(undefined, 'wa-newkey');

            expect(context.secrets.store).toHaveBeenCalledWith('waddleai.apiKey', 'wa-newkey');
            localClient.dispose();
        });

        it('is a no-op when neither argument is provided', async () => {
            const baseURLBefore = mockAxiosInstance.defaults.baseURL;
            await client.updateConfiguration();
            expect(mockAxiosInstance.defaults.baseURL).toBe(baseURLBefore);
        });
    });

    describe('axios interceptors registered in the constructor', () => {
        it('request interceptor leaves Authorization unset when no API key loaded (no secret, no config)', async () => {
            const [onFulfilled] = mockAxiosInstance.interceptors.request.use.mock.calls[0];
            await Promise.resolve(); // let the constructor's loadApiKey() microtask settle (no key by default)

            const configWithoutKey = { headers: {} as Record<string, string> };
            expect(onFulfilled(configWithoutKey).headers['Authorization']).toBeUndefined();
        });

        it('request interceptor adds the Authorization header once an API key is loaded from the config fallback', async () => {
            (vscode.workspace.getConfiguration as jest.Mock).mockReturnValue({
                get: jest.fn((key: string) => (key === 'apiKey' ? 'wa-from-config' : undefined))
            });
            const localClient = new WaddleAIClient(makeContext());
            await Promise.resolve();
            await Promise.resolve(); // loadApiKey() has two sequential awaits to settle

            const [onFulfilled] = mockAxiosInstance.interceptors.request.use.mock.calls[
                mockAxiosInstance.interceptors.request.use.mock.calls.length - 1
            ];
            const config = { headers: {} as Record<string, string> };
            expect(onFulfilled(config).headers['Authorization']).toBe('Bearer wa-from-config');
            localClient.dispose();
        });

        it('request interceptor rejection handler propagates the error unchanged', async () => {
            const [, onRejected] = mockAxiosInstance.interceptors.request.use.mock.calls[0];
            const error = new Error('bad request config');
            await expect(onRejected(error)).rejects.toBe(error);
        });

        it('response interceptor passes through a successful response unchanged', () => {
            const [onFulfilled] = mockAxiosInstance.interceptors.response.use.mock.calls[0];
            const response = { status: 200, data: {} };
            expect(onFulfilled(response)).toBe(response);
        });

        it('response interceptor triggers handleAuthError on a 401 and still rejects', async () => {
            (vscode.window.showErrorMessage as jest.Mock).mockResolvedValue(undefined);
            const [, onRejected] = mockAxiosInstance.interceptors.response.use.mock.calls[0];
            const error = httpError(401);

            await expect(onRejected(error)).rejects.toBe(error);
            expect(vscode.window.showErrorMessage).toHaveBeenCalledTimes(1);
        });

        it('response interceptor does NOT call handleAuthError on a non-401 error', async () => {
            const [, onRejected] = mockAxiosInstance.interceptors.response.use.mock.calls[0];
            const error = httpError(500);

            await expect(onRejected(error)).rejects.toBe(error);
            expect(vscode.window.showErrorMessage).not.toHaveBeenCalled();
        });
    });

    describe('handleAuthError() via a 401 from getAvailableModels()', () => {
        it('selecting "Update API Key" runs the setApiKey command and reloads the key', async () => {
            (vscode.window.showErrorMessage as jest.Mock).mockResolvedValue('Update API Key');
            mockAxiosInstance.get.mockRejectedValueOnce(httpError(401));

            await client.getAvailableModels();
            await new Promise((resolve) => setImmediate(resolve));

            expect(vscode.commands.executeCommand).toHaveBeenCalledWith('waddleai.setApiKey');
            // loadApiKey() re-reads from secret storage -- called once at
            // construction and once more here.
            expect((context.secrets.get as jest.Mock).mock.calls.length).toBeGreaterThanOrEqual(2);
        });

        it('dismissing the prompt (Cancel) does not run the setApiKey command', async () => {
            (vscode.window.showErrorMessage as jest.Mock).mockResolvedValue('Cancel');
            mockAxiosInstance.get.mockRejectedValueOnce(httpError(401));

            await client.getAvailableModels();
            await new Promise((resolve) => setImmediate(resolve));

            expect(vscode.commands.executeCommand).not.toHaveBeenCalled();
        });
    });

    describe('dispose()', () => {
        it('stops any in-flight model-fetch retry loop', async () => {
            jest.useFakeTimers();
            (vscode.window.showWarningMessage as jest.Mock).mockResolvedValue(undefined);
            mockAxiosInstance.get.mockRejectedValue(networkError());

            await client.getAvailableModels();
            client.dispose();

            await jest.advanceTimersByTimeAsync(60000);
            // Only the original call -- disposing stopped the backoff loop.
            expect(mockAxiosInstance.get).toHaveBeenCalledTimes(1);
        });
    });
});
