import * as vscode from 'vscode';
import { ollamaClient } from '../api/ollamaClient';
import { getConfiguration } from '../config/configuration';
import { buildCompletionPrompt } from '../utils/promptBuilder';

export class InlineCompletionProvider implements vscode.InlineCompletionItemProvider {
    private lastRequestTime: number = 0;
    private pendingRequest: AbortController | null = null;

    async provideInlineCompletionItems(
        document: vscode.TextDocument,
        position: vscode.Position,
        _context: vscode.InlineCompletionContext,
        token: vscode.CancellationToken
    ): Promise<vscode.InlineCompletionItem[] | undefined> {
        try {
            // 1. Check if inline completion is enabled in config
            const config = getConfiguration();
            if (!config.enableInlineCompletion) {
                return undefined;
            }

            // 2. Check if language is supported
            const supportedLanguages = config.supportedLanguages || [];
            if (supportedLanguages.length > 0 && !supportedLanguages.includes(document.languageId)) {
                return undefined;
            }

            // 3. Debounce requests based on config.inlineCompletionDebounceMs
            const now = Date.now();
            const debounceMs = config.inlineCompletionDebounceMs || 300;
            if (now - this.lastRequestTime < debounceMs) {
                return undefined;
            }
            this.lastRequestTime = now;

            // 4. Cancel any pending request
            if (this.pendingRequest) {
                this.pendingRequest.abort();
                this.pendingRequest = null;
            }

            // 5. Check if Ollama is available
            try {
                const isAvailable = await ollamaClient.isAvailable();
                if (!isAvailable) {
                    return undefined;
                }
            } catch (error) {
                return undefined;
            }

            // 6. Get context lines from document (prefix before cursor, suffix after)
            const lineCount = document.lineCount;
            const currentLine = position.line;
            const contextLines = config.contextLines || 50;

            // Get prefix (lines before cursor)
            const prefixStartLine = Math.max(0, currentLine - contextLines);
            const prefixRange = new vscode.Range(
                new vscode.Position(prefixStartLine, 0),
                position
            );
            const prefix = document.getText(prefixRange);

            // Get suffix (lines after cursor)
            const suffixEndLine = Math.min(lineCount - 1, currentLine + contextLines);
            const suffixRange = new vscode.Range(
                position,
                new vscode.Position(suffixEndLine, document.lineAt(suffixEndLine).text.length)
            );
            const suffix = document.getText(suffixRange);

            // 7. Build prompt using buildCompletionPrompt
            const prompt = buildCompletionPrompt(prefix, suffix, document.languageId);

            // 8. Check cancellation token
            if (token.isCancellationRequested) {
                return undefined;
            }

            // Create abort controller for this request
            this.pendingRequest = new AbortController();
            const abortController = this.pendingRequest;

            // Handle cancellation token
            token.onCancellationRequested(() => {
                if (abortController) {
                    abortController.abort();
                }
            });

            // 9. Generate completion using ollamaClient.generate() (non-streaming for speed)
            const response = await ollamaClient.generate({
                model: config.defaultModel,
                prompt,
                stream: false,
                options: {
                    temperature: 0.2,
                    num_predict: 256,
                    stop: ['\n\n', '```', '"""', "'''"]
                }
            });

            // Clear pending request
            this.pendingRequest = null;

            // Check cancellation again
            if (token.isCancellationRequested) {
                return undefined;
            }

            // 10. Clean up completion (remove markdown fences, limit lines)
            const cleanedCompletion = this.cleanCompletion(response, document.languageId);
            if (!cleanedCompletion) {
                return undefined;
            }

            // 11. Return InlineCompletionItem array
            const item = new vscode.InlineCompletionItem(cleanedCompletion);
            return [item];

        } catch (error) {
            // Silently fail for inline completions to avoid disrupting the user
            console.error('Inline completion error:', error);
            return undefined;
        }
    }

    private cleanCompletion(completion: string, _language: string): string | undefined {
        if (!completion || typeof completion !== 'string') {
            return undefined;
        }

        let cleaned = completion;

        // Remove markdown code fences
        // Remove opening fence with optional language identifier
        cleaned = cleaned.replace(/^```[a-zA-Z]*\n?/, '');
        // Remove closing fence
        cleaned = cleaned.replace(/\n?```\s*$/, '');

        // Trim whitespace
        cleaned = cleaned.trim();

        // Limit to ~10 lines
        const lines = cleaned.split('\n');
        if (lines.length > 10) {
            cleaned = lines.slice(0, 10).join('\n');
        }

        // Return undefined if empty
        if (cleaned.length === 0) {
            return undefined;
        }

        return cleaned;
    }
}
