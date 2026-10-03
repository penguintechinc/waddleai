/**
 * Minimal hand-written mock of the `vscode` module surface this extension
 * actually calls, for unit tests run under plain Jest/Node (the real
 * `vscode` module only resolves inside a running VS Code extension host).
 * Extend as new `vscode.*` calls are added -- keep it to what's used, not a
 * full API surface.
 */

export const window = {
    showWarningMessage: jest.fn(),
    showErrorMessage: jest.fn(),
    showInformationMessage: jest.fn(),
    showInputBox: jest.fn(),
    showQuickPick: jest.fn(),
    withProgress: jest.fn(),
    createWebviewPanel: jest.fn()
};

export const workspace = {
    getConfiguration: jest.fn()
};

export const commands = {
    executeCommand: jest.fn(),
    registerCommand: jest.fn()
};

export const authentication = {
    registerAuthenticationProvider: jest.fn()
};

export enum ConfigurationTarget {
    Global = 1,
    Workspace = 2,
    WorkspaceFolder = 3
}

export enum ProgressLocation {
    SourceControl = 1,
    Window = 10,
    Notification = 15
}

export class EventEmitter<T> {
    private listeners: Array<(e: T) => void> = [];
    event = (listener: (e: T) => void) => {
        this.listeners.push(listener);
        return { dispose: () => undefined };
    };
    fire(data: T) {
        this.listeners.forEach((listener) => listener(data));
    }
}

export const Uri = {
    joinPath: jest.fn()
};
