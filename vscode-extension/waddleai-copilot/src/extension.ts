import * as vscode from 'vscode';
import { WaddleAIChatParticipant } from './chatParticipant';
import { WaddleAIClient, WaddleAIUsageSummary } from './waddleaiClient';
import { AuthenticationProvider } from './authProvider';

let waddleAIClient: WaddleAIClient;
let authProvider: AuthenticationProvider;

export function activate(context: vscode.ExtensionContext) {
    console.log('WaddleAI Copilot extension is now active!');

    // Initialize authentication provider
    authProvider = new AuthenticationProvider(context);

    // Initialize WaddleAI client
    waddleAIClient = new WaddleAIClient(context);

    // Register WaddleAI chat participant
    const participant = new WaddleAIChatParticipant(waddleAIClient, context);
    const chatParticipant = vscode.chat.createChatParticipant('waddleai', participant.handleRequest.bind(participant));

    chatParticipant.iconPath = vscode.Uri.joinPath(context.extensionUri, 'media', 'icon.svg');
    chatParticipant.requestHandler = participant.handleRequest.bind(participant);

    context.subscriptions.push(chatParticipant);

    // Register commands
    registerCommands(context);

    // Show welcome message on first activation
    const hasShownWelcome = context.globalState.get('waddleai.welcomeShown');
    if (!hasShownWelcome) {
        showWelcomeMessage(context);
    }

    // Auto-configure if API key is not set
    const config = vscode.workspace.getConfiguration('waddleai');
    const apiKey = config.get<string>('apiKey');
    if (!apiKey) {
        vscode.window.showInformationMessage(
            'WaddleAI: Please set your API key to start using WaddleAI in Copilot Chat',
            'Set API Key'
        ).then(selection => {
            if (selection === 'Set API Key') {
                vscode.commands.executeCommand('waddleai.setApiKey');
            }
        });
    }
}

function registerCommands(context: vscode.ExtensionContext) {
    // Set API Key command
    const setApiKeyCommand = vscode.commands.registerCommand('waddleai.setApiKey', async () => {
        const apiKey = await vscode.window.showInputBox({
            prompt: 'Enter your WaddleAI API key',
            password: true,
            placeHolder: 'wa-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx',
            validateInput: (value) => {
                if (!value) {
                    return 'API key is required';
                }
                if (!value.startsWith('wa-')) {
                    return 'Invalid API key format. Should start with "wa-"';
                }
                return null;
            }
        });

        if (apiKey) {
            const config = vscode.workspace.getConfiguration('waddleai');
            await config.update('apiKey', apiKey, vscode.ConfigurationTarget.Global);

            // Store securely in secret storage
            await context.secrets.store('waddleai.apiKey', apiKey);

            vscode.window.showInformationMessage('WaddleAI API key saved successfully!');

            // Test connection
            vscode.commands.executeCommand('waddleai.testConnection');
        }
    });

    // Select Model command
    const selectModelCommand = vscode.commands.registerCommand('waddleai.selectModel', async () => {
        // Fetch available models from WaddleAI
        try {
            const models = await waddleAIClient.getAvailableModels();
            const modelNames = models.map(m => m.id);

            const selected = await vscode.window.showQuickPick(modelNames, {
                placeHolder: 'Select a model to use with WaddleAI',
                title: 'WaddleAI Model Selection'
            });

            if (selected) {
                const config = vscode.workspace.getConfiguration('waddleai');
                await config.update('defaultModel', selected, vscode.ConfigurationTarget.Global);
                vscode.window.showInformationMessage(`WaddleAI: Model set to ${selected}`);
            }
        } catch (error: any) {
            vscode.window.showErrorMessage(`Failed to fetch models: ${error.message}`);
        }
    });

    // Test Connection command
    const testConnectionCommand = vscode.commands.registerCommand('waddleai.testConnection', async () => {
        const progress = vscode.window.withProgress({
            location: vscode.ProgressLocation.Notification,
            title: 'Testing WaddleAI connection...',
            cancellable: false
        }, async () => {
            try {
                const health = await waddleAIClient.testConnection();
                if (health.ready) {
                    vscode.window.showInformationMessage('✅ WaddleAI connection successful!');
                    return true;
                } else {
                    vscode.window.showWarningMessage('⚠️ WaddleAI is not ready (database dependency unhealthy)');
                    return false;
                }
            } catch (error: any) {
                vscode.window.showErrorMessage(`❌ WaddleAI connection failed: ${error.message}`);
                return false;
            }
        });

        return progress;
    });

    // Show Usage command
    const showUsageCommand = vscode.commands.registerCommand('waddleai.showUsage', async () => {
        try {
            const usage = await waddleAIClient.getUsage();

            const panel = vscode.window.createWebviewPanel(
                'waddleaiUsage',
                'WaddleAI Token Usage',
                vscode.ViewColumn.One,
                {
                    enableScripts: true
                }
            );

            panel.webview.html = getUsageWebviewContent(usage);
        } catch (error: any) {
            vscode.window.showErrorMessage(`Failed to fetch usage: ${error.message}`);
        }
    });

    // Start New Conversation command. WaddleAI's proxy-memory layers key off
    // the X-WaddleAI-Session header (spec §6A); there is no server-side
    // "delete this session's memory" route today, so this resets the
    // client's own session id rather than calling a route that doesn't exist.
    const clearMemoryCommand = vscode.commands.registerCommand('waddleai.clearMemory', async () => {
        const confirm = await vscode.window.showWarningMessage(
            'Start a new conversation? Previous turns will no longer be included as context.',
            'Yes', 'No'
        );

        if (confirm === 'Yes') {
            waddleAIClient.resetSession();
            vscode.window.showInformationMessage('Started a new WaddleAI conversation');
        }
    });

    context.subscriptions.push(
        setApiKeyCommand,
        selectModelCommand,
        testConnectionCommand,
        showUsageCommand,
        clearMemoryCommand
    );
}

function showWelcomeMessage(context: vscode.ExtensionContext) {
    const message = `Welcome to WaddleAI for Copilot Chat! 🎉

    WaddleAI is now available as a language model provider in VS Code.
    You can use it in Copilot Chat by selecting "WaddleAI" from the model dropdown.

    To get started:
    1. Set your API key: Command Palette > "WaddleAI: Set API Key"
    2. Select a model: Command Palette > "WaddleAI: Select Model"
    3. Open Copilot Chat and select WaddleAI as your model provider
    `;

    vscode.window.showInformationMessage(message, 'Get Started', 'Later').then(selection => {
        if (selection === 'Get Started') {
            vscode.commands.executeCommand('waddleai.setApiKey');
        }
    });

    context.globalState.update('waddleai.welcomeShown', true);
}

function quotaPercent(used: number, limit: number): number {
    if (!limit) {
        return 0;
    }
    return Math.min(100, Math.max(0, (used / limit) * 100));
}

/**
 * `llm_breakdown` keys are model names that ultimately trace back to the
 * `model` field of a chat request (server-passed-through, not sanitized
 * for HTML) -- this webview interpolates them into an HTML string, so
 * they're escaped the same as any other untrusted string reaching the UI
 * rather than trusted just because they came from our own API.
 */
function escapeHtml(value: string): string {
    return value
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function renderModelBreakdown(breakdown: Record<string, { input: number; output: number }>): string {
    const models = Object.keys(breakdown);
    if (models.length === 0) {
        return '<div class="stat-title">No per-model breakdown for this window yet.</div>';
    }
    return models
        .map((model) => {
            const { input, output } = breakdown[model];
            return `<div class="model-row"><span class="model-name">${escapeHtml(model)}</span><span>${input.toLocaleString()} in / ${output.toLocaleString()} out</span></div>`;
        })
        .join('');
}

function getUsageWebviewContent(usage: WaddleAIUsageSummary): string {
    const dailyPct = quotaPercent(usage.daily.used, usage.daily.limit);
    const monthlyPct = quotaPercent(usage.monthly.used, usage.monthly.limit);
    return `
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>WaddleAI Usage</title>
        <style>
            body {
                font-family: var(--vscode-font-family);
                color: var(--vscode-foreground);
                background-color: var(--vscode-editor-background);
                padding: 20px;
            }
            .stat-card {
                background: var(--vscode-editor-inactiveSelectionBackground);
                border-radius: 8px;
                padding: 15px;
                margin-bottom: 15px;
            }
            .stat-title {
                font-size: 14px;
                color: var(--vscode-descriptionForeground);
                margin-bottom: 5px;
            }
            .stat-value {
                font-size: 24px;
                font-weight: bold;
                color: var(--vscode-editor-foreground);
            }
            .progress-bar {
                width: 100%;
                height: 20px;
                background: var(--vscode-input-background);
                border-radius: 10px;
                overflow: hidden;
                margin-top: 10px;
            }
            .progress-fill {
                height: 100%;
                background: var(--vscode-progressBar-background);
                transition: width 0.3s ease;
            }
            h1 {
                color: var(--vscode-editor-foreground);
                border-bottom: 1px solid var(--vscode-panel-border);
                padding-bottom: 10px;
            }
            .model-row {
                display: flex;
                justify-content: space-between;
                padding: 4px 0;
                font-size: 13px;
            }
            .model-name {
                font-family: var(--vscode-editor-font-family);
            }
        </style>
    </head>
    <body>
        <h1>🚀 WaddleAI Token Usage</h1>

        <div class="stat-card">
            <div class="stat-title">Total WaddleAI Tokens Used (last 30 days)</div>
            <div class="stat-value">${usage.total_waddleai_tokens.toLocaleString()}</div>
        </div>

        <div class="stat-card">
            <div class="stat-title">Total Requests (last 30 days)</div>
            <div class="stat-value">${usage.total_requests.toLocaleString()}</div>
        </div>

        <div class="stat-card">
            <div class="stat-title">Daily Quota</div>
            <div class="stat-value">${usage.daily.used.toLocaleString()} / ${usage.daily.limit.toLocaleString()}</div>
            <div class="progress-bar">
                <div class="progress-fill" style="width: ${dailyPct}%"></div>
            </div>
        </div>

        <div class="stat-card">
            <div class="stat-title">Monthly Quota</div>
            <div class="stat-value">${usage.monthly.used.toLocaleString()} / ${usage.monthly.limit.toLocaleString()}</div>
            <div class="progress-bar">
                <div class="progress-fill" style="width: ${monthlyPct}%"></div>
            </div>
        </div>

        <div class="stat-card">
            <div class="stat-title">Average Daily Usage</div>
            <div class="stat-value">${usage.average_daily.toLocaleString()}</div>
        </div>

        <div class="stat-card">
            <div class="stat-title">Model Breakdown (last 30 days)</div>
            ${renderModelBreakdown(usage.llm_breakdown)}
        </div>
    </body>
    </html>
    `;
}

export function deactivate() {
    // Clean up resources
    if (waddleAIClient) {
        waddleAIClient.dispose();
    }
}
