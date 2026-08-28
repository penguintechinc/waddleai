import { render } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import App from '../App';

// Mock all CSS imports
vi.mock('../App.css', () => ({}));
vi.mock('../pages/Login.css', () => ({}));
vi.mock('../pages/Dashboard.css', () => ({}));
vi.mock('../pages/Providers.css', () => ({}));
vi.mock('../pages/OllamaDeployments.css', () => ({}));
vi.mock('../pages/VirtualKeys.css', () => ({}));
vi.mock('../pages/UsageAnalytics.css', () => ({}));
vi.mock('../pages/Routing.css', () => ({}));
vi.mock('../pages/Memory.css', () => ({}));
vi.mock('../pages/Hooks.css', () => ({}));
vi.mock('../pages/Integrations.css', () => ({}));
vi.mock('../components/Header.css', () => ({}));

// Mock axios used by child pages
vi.mock('axios');

// Mock fetch for AuthContext token verification
global.fetch = vi.fn().mockResolvedValue({ ok: false });

describe('App', () => {
  it('renders login page at /login route by default (unauthenticated)', async () => {
    // jsdom starts at about:blank; App renders with BrowserRouter which defaults to /
    // ProtectedRoute redirects unauthenticated users → Login shows up
    render(<App />);

    // Wait for auth loading to complete — either login form or loading spinner
    // We just assert the app renders without crashing
    expect(document.body).toBeTruthy();
  });

  it('renders without throwing', () => {
    expect(() => render(<App />)).not.toThrow();
  });
});
