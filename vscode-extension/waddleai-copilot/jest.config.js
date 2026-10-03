/** @type {import('jest').Config} */
module.exports = {
    preset: 'ts-jest',
    testEnvironment: 'node',
    rootDir: '.',
    testMatch: ['<rootDir>/src/test/**/*.test.ts'],
    moduleFileExtensions: ['ts', 'js'],
    // The extension's only runtime dependency it can't unit-test against
    // directly is the `vscode` module, which only exists inside a running
    // VS Code host. Map it to a minimal hand-written mock instead of
    // pulling in @vscode/test-electron for what is otherwise pure logic.
    moduleNameMapper: {
        '^vscode$': '<rootDir>/src/test/__mocks__/vscode.ts'
    },
    collectCoverageFrom: [
        'src/retryBackoff.ts',
        'src/waddleaiClient.ts'
    ],
    coverageThreshold: {
        global: {
            lines: 90,
            branches: 90,
            functions: 90,
            statements: 90
        }
    }
};
