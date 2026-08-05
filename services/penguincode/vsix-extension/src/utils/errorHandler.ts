import * as vscode from 'vscode';

/**
 * Handles errors by displaying them to the user via VS Code's error message dialog.
 * Logs the error to the console for debugging purposes.
 *
 * @param error - The error object (can be any type)
 * @param context - A string describing where the error occurred
 */
export function handleError(error: unknown, context: string): void {
	const message = formatErrorMessage(error);
	const fullMessage = `Error in ${context}: ${message}`;

	console.error(fullMessage, error);
	vscode.window.showErrorMessage(fullMessage);
}

/**
 * Formats an error object of unknown type into a readable string.
 * Handles Error objects, strings, and other types gracefully.
 *
 * @param error - The error object to format (can be any type)
 * @returns A formatted error message string
 */
export function formatErrorMessage(error: unknown): string {
	// Handle null or undefined
	if (error === null || error === undefined) {
		return 'An unknown error occurred';
	}

	// Handle Error objects
	if (error instanceof Error) {
		return error.message;
	}

	// Handle string errors
	if (typeof error === 'string') {
		return error;
	}

	// Handle objects with message property
	if (typeof error === 'object' && 'message' in error) {
		const msg = error.message;
		if (typeof msg === 'string') {
			return msg;
		}
	}

	// Handle objects with toString() method
	if (typeof error === 'object' && error !== null) {
		try {
			const str = error.toString();
			if (str && str !== '[object Object]') {
				return str;
			}
		} catch {
			// Continue to fallback
		}
	}

	// Fallback: convert to string
	try {
		return String(error);
	} catch {
		return 'An unknown error occurred';
	}
}

/**
 * Checks if an error is related to Ollama connection issues.
 * Identifies common connection error patterns such as:
 * - Network errors (ECONNREFUSED, ENOTFOUND, ETIMEDOUT)
 * - HTTP errors (connection, timeout, unreachable)
 * - Ollama service unavailable
 *
 * @param error - The error object to check (can be any type)
 * @returns true if the error appears to be a connection error, false otherwise
 */
export function isOllamaConnectionError(error: unknown): boolean {
	// Handle null or undefined
	if (error === null || error === undefined) {
		return false;
	}

	// Check string error messages
	let errorString = '';
	if (typeof error === 'string') {
		errorString = error.toLowerCase();
	} else if (error instanceof Error) {
		errorString = error.message.toLowerCase();
	} else if (typeof error === 'object' && 'message' in error) {
		const msg = error.message;
		if (typeof msg === 'string') {
			errorString = msg.toLowerCase();
		}
	}

	// Check for common connection error patterns
	const connectionErrorPatterns = [
		'econnrefused',      // Connection refused
		'enotfound',         // DNS resolution failed
		'etimedout',         // Connection timeout
		'ehostunreach',      // Host unreachable
		'enetunreach',       // Network unreachable
		'econnreset',        // Connection reset
		'connection refused', // Human-readable form
		'connection timeout', // Human-readable form
		'getaddrinfo',       // DNS lookup error
		'connect econnrefused', // Node.js error format
		'socket hang up',    // Connection dropped
		'ollama',            // Contains ollama reference
		'localhost:11434',   // Default ollama port
		'127.0.0.1:11434',   // Default ollama port IP
		'unavailable',       // Service unavailable
		'unreachable',       // Service unreachable
	];

	return connectionErrorPatterns.some(pattern => errorString.includes(pattern));
}
