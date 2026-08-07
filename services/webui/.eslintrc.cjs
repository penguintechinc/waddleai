module.exports = {
  root: true,
  env: { browser: true, es2020: true },
  extends: [
    'eslint:recommended',
    'plugin:react/recommended',
    'plugin:react/jsx-runtime',
    'plugin:react-hooks/recommended',
  ],
  ignorePatterns: ['dist', '.eslintrc.cjs'],
  parserOptions: { ecmaVersion: 'latest', sourceType: 'module' },
  settings: { react: { version: '18.2' } },
  plugins: ['react-refresh'],
  rules: {
    'react-refresh/only-export-components': [
      'warn',
      { allowConstantExport: true },
    ],
    'react/prop-types': 'off',
  },
  overrides: [
    {
      // Vitest runs test files under Node, where `global` is the runtime
      // object tests stub (e.g. `global.fetch = vi.fn()`) — distinct from
      // the jsdom `window`/`browser` globals declared above.
      files: ['src/test/**/*.{js,jsx}'],
      env: { node: true },
    },
  ],
}
