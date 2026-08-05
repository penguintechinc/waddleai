# WaddleAI E2E Tests

Playwright E2E tests for WaddleAI webui against the beta cluster.

## Setup

```bash
npm install
```

## Running Tests

```bash
# Run all tests
npm test

# Run with UI mode (interactive)
npm run test:ui

# Run in debug mode
npm run test:debug

# View test report
npm run report
```

## Configuration

- **Base URL**: `https://dal2.penguintech.cloud` (internal LB)
- **Host Header**: `waddleai.penguintech.cloud`
- **HTTPS Verification**: Disabled (cert mismatch expected)
- **Artifacts**: `/tmp/playwright-waddleai/`
- **Timeout**: 30 seconds per test
- **Retries**: 1 (CI: 2)

## Test Coverage

### Smoke Tests
1. **Login Page**
   - Renders with logo, username/password fields, sign-in button
   - Invalid credentials show error message
   - Valid credentials (admin/password) redirect to dashboard

2. **Protected Routes**
   - Unauthenticated access to `/`, `/providers`, `/keys`, `/analytics`, `/ollama` redirects to `/login`

3. **Authenticated User - Dashboard**
   - Dashboard loads after login with visible content
   - Navigation menu present with expected links (Dashboard, Providers, Ollama, Virtual Keys, Analytics)
   - Each protected page loads without JS errors
   - Navigation between pages works without errors

4. **Session Management**
   - Admin session persists across page navigation
   - User stays authenticated when navigating between routes

## Credentials

Default admin credentials:
- Username: `admin`
- Password: `password`

## Notes

- Console warnings/errors do NOT cause test failures (minor warnings are acceptable)
- Tests use semantic selectors (role, text, placeholder) with fallbacks to data-testid
- Login helper function (`loginAsAdmin`) handles credential input across different form implementations
- Screenshots and videos captured only on failure
- HTML report generated at `/tmp/playwright-waddleai/report/`
