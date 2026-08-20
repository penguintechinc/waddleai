import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import Integrations from '../pages/Integrations';
import { useAuth } from '../contexts/AuthContext';

// Mock CSS import
vi.mock('../pages/Integrations.css', () => ({}));

// Mock axios
vi.mock('axios');
import axios from 'axios';

// Integrations.jsx sources role from AuthContext, mirroring Routing.jsx.
vi.mock('../contexts/AuthContext', () => ({
  useAuth: vi.fn(),
}));

const adminUser = { id: 1, username: 'admin', organization_id: 1, role: 'admin' };
const viewerUser = { id: 2, username: 'viewer', organization_id: 1, role: 'viewer' };

const mockSharedEndpoint = {
  id: 1,
  org_id: 1,
  name: 'Elder Docs MCP',
  url: 'https://mcp.elder.example.com/mcp',
  transport: 'streamable_http',
  auth_type: 'header',
  auth_config: { header_name: 'Authorization', header_value: 'sk-1****abcd' },
  identity_mode: 'shared',
  namespace: 'elder',
  credentials_ref: null,
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
};

const mockPerUserEndpoint = {
  id: 2,
  org_id: 1,
  name: 'Notion MCP',
  url: 'https://mcp.notion.example.com/mcp',
  transport: 'streamable_http',
  auth_type: 'oauth2_auth_code',
  auth_config: {
    authorization_endpoint: 'https://notion.example.com/oauth2/authorize',
    token_endpoint: 'https://notion.example.com/oauth2/token',
    client_id: 'abc123',
  },
  identity_mode: 'per_user',
  namespace: 'notion',
  credentials_ref: null,
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
};

const mockEndpointsResponse = {
  status: 'success',
  data: [mockSharedEndpoint, mockPerUserEndpoint],
  meta: { total: 2, timestamp: '2026-01-01T00:00:00Z' },
};

describe('Integrations page - admin', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    useAuth.mockReturnValue({ user: adminUser });
    axios.get.mockResolvedValue({ data: mockEndpointsResponse });
  });

  it('shows loading state initially', () => {
    axios.get.mockReturnValue(new Promise(() => {}));
    render(<Integrations />);
    expect(screen.getByText('Loading integrations...')).toBeInTheDocument();
  });

  it('renders page header and fetches endpoints', async () => {
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('Integrations')).toBeInTheDocument();
    });
    expect(axios.get).toHaveBeenCalledWith('/api/v1/integrations/mcp-endpoints');
  });

  it('lists registered endpoints with masked auth config', async () => {
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('Elder Docs MCP')).toBeInTheDocument();
      expect(screen.getByText('Notion MCP')).toBeInTheDocument();
    });
    // The masked value from the server is displayed as-is -- never the real secret.
    expect(screen.queryByText('sk-1****abcd')).not.toBeInTheDocument(); // not rendered raw in the card
  });

  it('shows the empty state when no endpoints are registered', async () => {
    axios.get.mockResolvedValue({ data: { status: 'success', data: [], meta: { total: 0 } } });
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('No MCP endpoints registered yet')).toBeInTheDocument();
    });
  });

  it('shows an error message when fetching endpoints fails', async () => {
    axios.get.mockRejectedValue({ response: { data: { error: 'not_found' } } });
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('not_found')).toBeInTheDocument();
    });
  });

  it('shows generic error when fetch fails without a response error field', async () => {
    axios.get.mockRejectedValue(new Error('Network error'));
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('Failed to fetch MCP endpoints')).toBeInTheDocument();
    });
  });

  it('opens the register modal and creates an endpoint with a "none" auth_type', async () => {
    axios.post.mockResolvedValue({ data: { status: 'success', data: mockSharedEndpoint } });
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('Elder Docs MCP')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ Register MCP Endpoint' }));
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'New MCP' } });
    fireEvent.change(screen.getByLabelText('Namespace'), { target: { value: 'newmcp' } });
    fireEvent.change(screen.getByLabelText('URL'), {
      target: { value: 'https://new.example.com/mcp' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Register Endpoint' }));

    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith('/api/v1/integrations/mcp-endpoints', {
        name: 'New MCP',
        url: 'https://new.example.com/mcp',
        transport: 'streamable_http',
        auth_type: 'none',
        identity_mode: 'shared',
        namespace: 'newmcp',
        credentials_ref: '',
        status: 'active',
        auth_config: {},
      });
    });
    await waitFor(() => {
      expect(screen.getByText('MCP endpoint registered successfully')).toBeInTheDocument();
    });
  });

  it('renders header auth fields and submits header_value as part of auth_config', async () => {
    axios.post.mockResolvedValue({ data: { status: 'success', data: mockSharedEndpoint } });
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('Elder Docs MCP')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ Register MCP Endpoint' }));
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Header MCP' } });
    fireEvent.change(screen.getByLabelText('Namespace'), { target: { value: 'headermcp' } });
    fireEvent.change(screen.getByLabelText('URL'), {
      target: { value: 'https://header.example.com/mcp' },
    });
    fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'header' } });

    expect(screen.getByLabelText('Header Name')).toHaveValue('Authorization');
    fireEvent.change(screen.getByLabelText('Header Value'), { target: { value: 'Bearer secret-token' } });

    fireEvent.click(screen.getByRole('button', { name: 'Register Endpoint' }));

    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith(
        '/api/v1/integrations/mcp-endpoints',
        expect.objectContaining({
          auth_config: { header_name: 'Authorization', header_value: 'Bearer secret-token' },
        })
      );
    });
  });

  it('renders oauth2_client_credentials auth fields and submits them as part of auth_config', async () => {
    axios.post.mockResolvedValue({ data: { status: 'success', data: mockSharedEndpoint } });
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('Elder Docs MCP')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ Register MCP Endpoint' }));
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'CC MCP' } });
    fireEvent.change(screen.getByLabelText('Namespace'), { target: { value: 'ccmcp' } });
    fireEvent.change(screen.getByLabelText('URL'), { target: { value: 'https://cc.example.com/mcp' } });
    fireEvent.change(screen.getByLabelText('Authentication'), {
      target: { value: 'oauth2_client_credentials' },
    });

    fireEvent.change(screen.getByLabelText('Token Endpoint'), {
      target: { value: 'https://idp.example.com/oauth2/token' },
    });
    fireEvent.change(screen.getByLabelText('Client ID'), { target: { value: 'client-abc' } });
    fireEvent.change(screen.getByLabelText('Client Secret'), { target: { value: 'shh-secret' } });
    fireEvent.change(screen.getByLabelText('Scope (optional)'), { target: { value: 'mcp:invoke' } });

    fireEvent.click(screen.getByRole('button', { name: 'Register Endpoint' }));

    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith(
        '/api/v1/integrations/mcp-endpoints',
        expect.objectContaining({
          auth_config: {
            token_endpoint: 'https://idp.example.com/oauth2/token',
            client_id: 'client-abc',
            client_secret: 'shh-secret',
            scope: 'mcp:invoke',
          },
        })
      );
    });
  });

  it('renders oauth2_auth_code auth fields (incl. DCR endpoint) and submits them as part of auth_config', async () => {
    axios.post.mockResolvedValue({ data: { status: 'success', data: mockPerUserEndpoint } });
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('Elder Docs MCP')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ Register MCP Endpoint' }));
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'AC MCP' } });
    fireEvent.change(screen.getByLabelText('Namespace'), { target: { value: 'acmcp' } });
    fireEvent.change(screen.getByLabelText('URL'), { target: { value: 'https://ac.example.com/mcp' } });
    fireEvent.change(screen.getByLabelText('Authentication'), {
      target: { value: 'oauth2_auth_code' },
    });

    fireEvent.change(screen.getByLabelText('Authorization Endpoint'), {
      target: { value: 'https://idp.example.com/oauth2/authorize' },
    });
    fireEvent.change(screen.getByLabelText('Token Endpoint'), {
      target: { value: 'https://idp.example.com/oauth2/token' },
    });
    fireEvent.change(screen.getByLabelText('Dynamic Client Registration Endpoint (optional)'), {
      target: { value: 'https://idp.example.com/oauth2/register' },
    });
    fireEvent.change(screen.getByLabelText('Client ID (optional if using DCR)'), {
      target: { value: 'client-xyz' },
    });
    fireEvent.change(screen.getByLabelText('Client Secret (optional if using DCR)'), {
      target: { value: 'dcr-secret' },
    });
    fireEvent.change(screen.getByLabelText('Scope (optional)'), { target: { value: 'openid mcp' } });

    fireEvent.click(screen.getByRole('button', { name: 'Register Endpoint' }));

    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith(
        '/api/v1/integrations/mcp-endpoints',
        expect.objectContaining({
          auth_config: {
            authorization_endpoint: 'https://idp.example.com/oauth2/authorize',
            token_endpoint: 'https://idp.example.com/oauth2/token',
            registration_endpoint: 'https://idp.example.com/oauth2/register',
            client_id: 'client-xyz',
            client_secret: 'dcr-secret',
            scope: 'openid mcp',
          },
        })
      );
    });
  });

  it('shows a namespace conflict error from the server', async () => {
    axios.post.mockRejectedValue({
      response: { data: { error: "an endpoint with namespace 'elder' already exists for this org" } },
    });
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('Elder Docs MCP')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ Register MCP Endpoint' }));
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Dup' } });
    fireEvent.change(screen.getByLabelText('Namespace'), { target: { value: 'elder' } });
    fireEvent.change(screen.getByLabelText('URL'), { target: { value: 'https://dup.example.com/mcp' } });
    fireEvent.click(screen.getByRole('button', { name: 'Register Endpoint' }));

    await waitFor(() => {
      expect(screen.getByText(/already exists for this org/)).toBeInTheDocument();
    });
  });

  it('cancels the register modal without submitting', async () => {
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('Elder Docs MCP')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: '+ Register MCP Endpoint' }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByLabelText('Name')).not.toBeInTheDocument();
    expect(axios.post).not.toHaveBeenCalled();
  });

  it('edits an endpoint without touching auth_config when not reconfiguring', async () => {
    axios.put.mockResolvedValue({ data: { status: 'success', data: mockSharedEndpoint } });
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('Elder Docs MCP')).toBeInTheDocument();
    });

    const editButtons = screen.getAllByRole('button', { name: 'Edit' });
    fireEvent.click(editButtons[0]);

    expect(screen.getByLabelText('Namespace')).toBeDisabled();
    // Auth fields are hidden until "reconfigure" is checked -- omitting
    // auth_config from the PUT body is what keeps the existing encrypted
    // secret intact server-side.
    expect(screen.queryByLabelText('Header Value')).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Elder Docs MCP (renamed)' } });
    fireEvent.click(screen.getByRole('button', { name: 'Update Endpoint' }));

    await waitFor(() => {
      expect(axios.put).toHaveBeenCalledWith(
        '/api/v1/integrations/mcp-endpoints/1',
        expect.not.objectContaining({ auth_config: expect.anything() })
      );
    });
    const [, putBody] = axios.put.mock.calls[0];
    expect(putBody.namespace).toBeUndefined();
    expect(putBody.name).toBe('Elder Docs MCP (renamed)');
  });

  it('requires re-entering the full auth_config when reconfiguring auth on edit', async () => {
    axios.put.mockResolvedValue({ data: { status: 'success', data: mockSharedEndpoint } });
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('Elder Docs MCP')).toBeInTheDocument();
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);
    fireEvent.click(screen.getByLabelText('Change authentication configuration'));

    expect(screen.getByLabelText('Header Value')).toHaveValue('');
    fireEvent.change(screen.getByLabelText('Header Value'), { target: { value: 'Bearer new-secret' } });
    fireEvent.click(screen.getByRole('button', { name: 'Update Endpoint' }));

    await waitFor(() => {
      expect(axios.put).toHaveBeenCalledWith(
        '/api/v1/integrations/mcp-endpoints/1',
        expect.objectContaining({
          auth_config: { header_name: 'Authorization', header_value: 'Bearer new-secret' },
        })
      );
    });
  });

  it('deletes an endpoint after confirmation', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    axios.delete.mockResolvedValue({ data: { status: 'success', data: { id: 1 } } });
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('Elder Docs MCP')).toBeInTheDocument();
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);

    await waitFor(() => {
      expect(axios.delete).toHaveBeenCalledWith('/api/v1/integrations/mcp-endpoints/1');
    });
    confirmSpy.mockRestore();
  });

  it('does not delete when confirmation is denied', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('Elder Docs MCP')).toBeInTheDocument();
    });
    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
    expect(axios.delete).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('shows Link My Account only for per_user endpoints', async () => {
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('Elder Docs MCP')).toBeInTheDocument();
    });
    const linkButtons = screen.getAllByRole('button', { name: 'Link My Account' });
    expect(linkButtons).toHaveLength(1);
  });

  it('opens a valid https authorization_url in a new tab', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => {});
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/integrations/mcp-endpoints/2/link') {
        return Promise.resolve({
          data: { status: 'success', data: { authorization_url: 'https://notion.example.com/authorize?x=1' } },
        });
      }
      return Promise.resolve({ data: mockEndpointsResponse });
    });
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Link My Account' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Link My Account' }));

    await waitFor(() => {
      expect(openSpy).toHaveBeenCalledWith(
        'https://notion.example.com/authorize?x=1',
        '_blank',
        'noopener,noreferrer'
      );
    });
    openSpy.mockRestore();
  });

  it('refuses to open a non-http(s) authorization_url', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => {});
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/integrations/mcp-endpoints/2/link') {
        return Promise.resolve({
          data: { status: 'success', data: { authorization_url: 'javascript:alert(1)' } },
        });
      }
      return Promise.resolve({ data: mockEndpointsResponse });
    });
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Link My Account' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Link My Account' }));

    await waitFor(() => {
      expect(screen.getByText(/not opening it/)).toBeInTheDocument();
    });
    expect(openSpy).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it('shows an error when the link flow request fails', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/integrations/mcp-endpoints/2/link') {
        return Promise.reject({ response: { data: { error: 'this endpoint is not configured for per_user identity' } } });
      }
      return Promise.resolve({ data: mockEndpointsResponse });
    });
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Link My Account' })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: 'Link My Account' }));
    await waitFor(() => {
      expect(screen.getByText('this endpoint is not configured for per_user identity')).toBeInTheDocument();
    });
  });
});

describe('Integrations page - non-admin (viewer)', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    useAuth.mockReturnValue({ user: viewerUser });
  });

  it('does not call the admin-only endpoints list route', async () => {
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText('Integrations')).toBeInTheDocument();
    });
    expect(axios.get).not.toHaveBeenCalledWith('/api/v1/integrations/mcp-endpoints');
  });

  it('shows the admin-required notice instead of a fabricated read-only list', async () => {
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByText(/Managing MCP endpoints requires Admin access/)).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: '+ Register MCP Endpoint' })).not.toBeInTheDocument();
  });
});

describe('Integrations page - OpenCode config (self-service, any role)', () => {
  const originalClipboardDescriptor = Object.getOwnPropertyDescriptor(window.navigator, 'clipboard');

  afterEach(() => {
    if (originalClipboardDescriptor) {
      Object.defineProperty(navigator, 'clipboard', originalClipboardDescriptor);
    } else {
      delete navigator.clipboard;
    }
  });

  beforeEach(() => {
    vi.resetAllMocks();
    useAuth.mockReturnValue({ user: viewerUser });
  });

  it('requires a virtual key before submitting', async () => {
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Generate Config' })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: 'Generate Config' }));
    await waitFor(() => {
      expect(screen.getByText('Paste one of your own virtual keys to generate a config')).toBeInTheDocument();
    });
    expect(axios.post).not.toHaveBeenCalled();
  });

  it('generates a config, never logs the raw key, and clears the input field', async () => {
    const consoleLogSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const config = {
      provider: {
        waddleai: {
          type: 'openai-compatible',
          baseURL: 'http://localhost:8000/v1',
          apiKey: 'wk-super-secret-value',
          models: 'http://localhost:8000/v1/models',
        },
      },
      mcp: {
        waddleai: {
          type: 'remote',
          url: 'http://localhost:8000/mcp',
          headers: { Authorization: 'Bearer wk-super-secret-value' },
        },
      },
    };
    axios.post.mockResolvedValue({ data: { status: 'success', data: config, meta: { key_id: 5 } } });

    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByLabelText('Virtual Key')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText('Virtual Key'), { target: { value: 'wk-super-secret-value' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate Config' }));

    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith('/api/v1/integrations/opencode-config', {
        virtual_key: 'wk-super-secret-value',
      });
    });
    await waitFor(() => {
      expect(screen.getByTestId('opencode-config-preview')).toBeInTheDocument();
    });

    expect(screen.getByLabelText('Virtual Key')).toHaveValue('');

    const loggedCalls = [...consoleLogSpy.mock.calls, ...consoleErrorSpy.mock.calls].flat();
    expect(loggedCalls.some((arg) => String(arg).includes('wk-super-secret-value'))).toBe(false);

    consoleLogSpy.mockRestore();
    consoleErrorSpy.mockRestore();
  });

  it('shows an error when the caller does not own the supplied key', async () => {
    axios.post.mockRejectedValue({
      response: { data: { error: 'virtual_key not recognized for this account' } },
    });
    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByLabelText('Virtual Key')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByLabelText('Virtual Key'), { target: { value: 'wk-not-mine' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate Config' }));
    await waitFor(() => {
      expect(screen.getByText('virtual_key not recognized for this account')).toBeInTheDocument();
    });
  });

  it('downloads the rendered config as opencode.json', async () => {
    const config = { provider: {}, mcp: {} };
    axios.post.mockResolvedValue({ data: { status: 'success', data: config, meta: {} } });
    const createObjectURL = vi.fn().mockReturnValue('blob:mock');
    const revokeObjectURL = vi.fn();
    global.URL.createObjectURL = createObjectURL;
    global.URL.revokeObjectURL = revokeObjectURL;
    const clickSpy = vi.fn();
    const originalCreateElement = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tag) => {
      const el = originalCreateElement(tag);
      if (tag === 'a') {
        el.click = clickSpy;
      }
      return el;
    });

    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByLabelText('Virtual Key')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByLabelText('Virtual Key'), { target: { value: 'wk-abc' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate Config' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Download opencode.json' })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: 'Download opencode.json' }));

    expect(createObjectURL).toHaveBeenCalled();
    expect(clickSpy).toHaveBeenCalled();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock');

    document.createElement.mockRestore();
  });

  it('copies the rendered config to the clipboard', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      configurable: true,
      writable: true,
    });
    const config = { provider: {}, mcp: {} };
    axios.post.mockResolvedValue({ data: { status: 'success', data: config, meta: {} } });

    render(<Integrations />);
    await waitFor(() => {
      expect(screen.getByLabelText('Virtual Key')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByLabelText('Virtual Key'), { target: { value: 'wk-abc' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate Config' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Copy to Clipboard' })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: 'Copy to Clipboard' }));

    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(JSON.stringify(config, null, 2));
    });
  });
});
