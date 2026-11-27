import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import Routing from '../pages/Routing';
import { useAuth } from '../contexts/AuthContext';

// Mock CSS import
vi.mock('../pages/Routing.css', () => ({}));

// Mock axios
vi.mock('axios');
import axios from 'axios';

// Mock the auth context: Routing.jsx sources organization_id from here, not
// a prop or a hardcoded default (see AuthContext.jsx / /auth/verify).
vi.mock('../contexts/AuthContext', () => ({
  useAuth: vi.fn(),
}));

const mockPolicy = {
  status: 'success',
  data: {
    organization_id: 1,
    mode: 'local_first',
    classifier_prompt: 'Route programming tasks to claude-3-sonnet.',
  },
  meta: { timestamp: '2026-01-01T00:00:00Z' },
};

describe('Routing page', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    useAuth.mockReturnValue({ user: { id: 1, username: 'admin', organization_id: 1 } });
    axios.get.mockResolvedValue({ data: mockPolicy });
  });

  it('shows loading state initially', () => {
    axios.get.mockReturnValue(new Promise(() => {}));
    render(<Routing />);
    expect(screen.getByText('Loading routing configuration...')).toBeInTheDocument();
  });

  it('shows loading state when organization_id is not yet available', () => {
    useAuth.mockReturnValue({ user: { id: 1, username: 'admin' } });
    render(<Routing />);
    expect(screen.getByText('Loading routing configuration...')).toBeInTheDocument();
    expect(axios.get).not.toHaveBeenCalled();
  });

  it('renders page header', async () => {
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByText('Routing Configuration')).toBeInTheDocument();
    });
  });

  it('loads and displays the current org-scoped policy classifier_prompt', async () => {
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByDisplayValue('Route programming tasks to claude-3-sonnet.')).toBeInTheDocument();
    });
    expect(axios.get).toHaveBeenCalledWith('/api/v1/routing/policies/1');
  });

  it('shows error message when fetch fails', async () => {
    axios.get.mockRejectedValue({ response: { data: { error: 'Access denied' } } });
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByText('Access denied')).toBeInTheDocument();
    });
  });

  it('shows generic error when no response error field', async () => {
    axios.get.mockRejectedValue(new Error('Network error'));
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByText('Failed to fetch routing policy')).toBeInTheDocument();
    });
  });

  it('dismisses error alert when close button clicked', async () => {
    axios.get.mockRejectedValue(new Error('error'));
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByText('Failed to fetch routing policy')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: '×' }));
    expect(screen.queryByText('Failed to fetch routing policy')).not.toBeInTheDocument();
  });

  it('saves the classifier_prompt via PUT to the org-scoped policy route', async () => {
    axios.put.mockResolvedValue({ data: { status: 'success', data: mockPolicy.data } });
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Routing Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Routing Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('Routing configuration saved successfully')).toBeInTheDocument();
    });

    expect(axios.put).toHaveBeenCalledWith('/api/v1/routing/policies/1', {
      classifier_prompt: 'Route programming tasks to claude-3-sonnet.',
    });
  });

  it('shows error when save fails', async () => {
    axios.put.mockRejectedValue({ response: { data: { error: 'Access denied' } } });
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Routing Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Routing Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('Access denied')).toBeInTheDocument();
    });
  });

  it('shows generic error when save fails without response error field', async () => {
    axios.put.mockRejectedValue(new Error('Network error'));
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Routing Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Routing Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('Failed to save routing configuration')).toBeInTheDocument();
    });
  });

  it('reloads current configuration when Reload Current clicked', async () => {
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Reload Current' })).toBeInTheDocument();
    });

    axios.get.mockClear();
    fireEvent.click(screen.getByRole('button', { name: 'Reload Current' }));

    await waitFor(() => {
      expect(axios.get).toHaveBeenCalledWith('/api/v1/routing/policies/1');
    });
  });

  it('falls back to an empty instructions field when classifier_prompt is empty', async () => {
    axios.get.mockResolvedValue({ data: { status: 'success', data: { organization_id: 1 }, meta: {} } });
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Routing Instructions')).toBeInTheDocument();
    });
    expect(screen.getByLabelText('Routing Instructions')).toHaveValue('');
  });

  it('falls back to an empty instructions field when response.data.data is entirely missing', async () => {
    axios.get.mockResolvedValue({ data: { status: 'success' } });
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Routing Instructions')).toBeInTheDocument();
    });
    expect(screen.getByLabelText('Routing Instructions')).toHaveValue('');
  });

  it('updates routing instructions textarea', async () => {
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Routing Instructions')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByLabelText('Routing Instructions'), { target: { value: 'New instructions' } });
    expect(screen.getByLabelText('Routing Instructions')).toHaveValue('New instructions');
  });

  it('dismisses success alert when close button clicked', async () => {
    axios.put.mockResolvedValue({ data: { status: 'success', data: mockPolicy.data } });
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Routing Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Routing Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('Routing configuration saved successfully')).toBeInTheDocument();
    });

    const successAlert = screen.getByText('Routing configuration saved successfully').closest('.alert');
    fireEvent.click(successAlert.querySelector('button'));
    expect(screen.queryByText('Routing configuration saved successfully')).not.toBeInTheDocument();
  });

  it('clears success message automatically after 3 seconds', async () => {
    axios.put.mockResolvedValue({ data: { status: 'success', data: mockPolicy.data } });
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Routing Configuration' })).toBeInTheDocument();
    });

    vi.useFakeTimers({ shouldAdvanceTime: false });
    fireEvent.click(screen.getByRole('button', { name: 'Save Routing Configuration' }));

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByText('Routing configuration saved successfully')).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(3000);
    });

    expect(screen.queryByText('Routing configuration saved successfully')).not.toBeInTheDocument();
    vi.useRealTimers();
  });
});

// ---------------------------------------------------------------------------
// Routing LLM Model selector (spec §7.2, §2.3) -- restored release-gate control
// ---------------------------------------------------------------------------

const mockAssignmentsWithRow = {
  status: 'success',
  data: [
    {
      id: 7,
      tool_type: 'routing-classifier',
      model_name: 'gemma4:e4b',
      scope: 'global',
      scope_ref: null,
    },
  ],
  meta: { total: 1, timestamp: '2026-01-01T00:00:00Z' },
};

const mockAssignmentsEmpty = {
  status: 'success',
  data: [],
  meta: { total: 0, timestamp: '2026-01-01T00:00:00Z' },
};

function mockGetByUrl({ assignments = mockAssignmentsWithRow } = {}) {
  axios.get.mockImplementation((url) => {
    if (url.startsWith('/api/v1/routing/policies/')) {
      return Promise.resolve({ data: mockPolicy });
    }
    if (url === '/api/v1/routing/assignments/') {
      return Promise.resolve({ data: assignments });
    }
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
}

describe('Routing page - admin: Routing LLM Model selector', () => {
  const adminUser = { id: 1, username: 'admin', organization_id: 1, role: 'admin' };

  beforeEach(() => {
    vi.resetAllMocks();
    useAuth.mockReturnValue({ user: adminUser });
  });

  it('loads the current routing-classifier model into the selector', async () => {
    mockGetByUrl();
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Routing LLM Model')).toHaveValue('gemma4:e4b');
    });
    expect(axios.get).toHaveBeenCalledWith(
      '/api/v1/routing/assignments/',
      expect.objectContaining({ params: { tool_type: 'routing-classifier' } })
    );
  });

  it('offers only Gemma 4 tags at or above the e4b minimum -- no e2b, no gemma4:2b, no Gemma 3', async () => {
    mockGetByUrl();
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Routing LLM Model')).toBeInTheDocument();
    });

    const values = screen.getAllByRole('option').map((option) => option.value);
    expect(values).toEqual(['gemma4:e4b', 'gemma4:12b', 'gemma4:26b', 'gemma4:31b']);
    expect(values).not.toContain('gemma4:e2b');
    expect(values).not.toContain('gemma4:2b');
    expect(values.some((v) => v.startsWith('gemma3'))).toBe(false);
  });

  it('surfaces a below-minimum stored tag as a disabled legacy option', async () => {
    mockGetByUrl({
      assignments: {
        status: 'success',
        data: [{ id: 7, tool_type: 'routing-classifier', model_name: 'gemma4:e2b', scope: 'global' }],
      },
    });
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Routing LLM Model')).toHaveValue('gemma4:e2b');
    });

    const legacy = screen
      .getAllByRole('option')
      .find((option) => option.value === 'gemma4:e2b');
    expect(legacy).toBeDefined();
    expect(legacy).toBeDisabled();
    expect(legacy.textContent).toContain('below the Gemma 4 E4B minimum');
  });

  it('saves the selected model via PUT when an assignment row already exists', async () => {
    mockGetByUrl();
    axios.put.mockResolvedValue({
      data: { status: 'success', data: { ...mockAssignmentsWithRow.data[0], model_name: 'gemma4:26b' } },
    });
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Routing LLM Model')).toHaveValue('gemma4:e4b');
    });

    fireEvent.change(screen.getByLabelText('Routing LLM Model'), {
      target: { value: 'gemma4:26b' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Routing LLM Model' }));

    await waitFor(() => {
      expect(axios.put).toHaveBeenCalledWith('/api/v1/routing/assignments/7', {
        model_name: 'gemma4:26b',
      });
    });
    await waitFor(() => {
      expect(screen.getByText('Routing LLM model saved successfully')).toBeInTheDocument();
    });
  });

  it('creates the assignment via POST when no row exists yet', async () => {
    mockGetByUrl({ assignments: mockAssignmentsEmpty });
    axios.post.mockResolvedValue({ data: { status: 'success', data: { id: 9, model_name: 'gemma4:e4b' } } });
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Routing LLM Model')).toHaveValue('gemma4:e4b');
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Routing LLM Model' }));

    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith('/api/v1/routing/assignments/', {
        tool_type: 'routing-classifier',
        model_name: 'gemma4:e4b',
        scope: 'global',
      });
    });
  });

  it('shows an error when saving the routing model fails', async () => {
    mockGetByUrl();
    axios.put.mockRejectedValue({ response: { data: { error: 'Access denied' } } });
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Routing LLM Model')).toHaveValue('gemma4:e4b');
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Routing LLM Model' }));

    await waitFor(() => {
      expect(screen.getByText('Access denied')).toBeInTheDocument();
    });
  });
});

describe('Routing page - non-admin: Routing LLM Model is read-only', () => {
  const viewerUser = { id: 2, username: 'viewer', organization_id: 1, role: 'viewer' };

  beforeEach(() => {
    vi.resetAllMocks();
    useAuth.mockReturnValue({ user: viewerUser });
  });

  it('shows the current model as read-only text, with no editable control', async () => {
    mockGetByUrl();
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByText('gemma4:e4b')).toBeInTheDocument();
    });
    expect(screen.queryByLabelText('Routing LLM Model')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save Routing LLM Model' })).not.toBeInTheDocument();
  });

  it('never shows the Test Routing Decision card to a non-admin', async () => {
    mockGetByUrl();
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByText('gemma4:e4b')).toBeInTheDocument();
    });
    expect(screen.queryByText('Test Routing Decision')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Test Routing Decision (dry-run) -- genuine RoutingEngine preview, admin-only
// ---------------------------------------------------------------------------

describe('Routing page - admin: Test Routing Decision', () => {
  const adminUser = { id: 1, username: 'admin', organization_id: 1, role: 'admin' };

  beforeEach(() => {
    vi.resetAllMocks();
    useAuth.mockReturnValue({ user: adminUser });
    mockGetByUrl();
  });

  it('is disabled until a prompt is entered', async () => {
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Run Test' })).toBeDisabled();
    });
    fireEvent.change(screen.getByLabelText('Sample Prompt'), { target: { value: 'hi' } });
    expect(screen.getByRole('button', { name: 'Run Test' })).not.toBeDisabled();
  });

  it('posts the prompt and optional tool_type, and renders the real (non-fabricated) decision', async () => {
    axios.post.mockImplementation((url) => {
      if (url === '/api/v1/routing/dry-run/') {
        return Promise.resolve({
          data: {
            status: 'success',
            data: {
              model: 'gpt-4o',
              fallback_chain: ['gpt-4o-mini'],
              routed_from: null,
              tool_type: 'code',
              tool_type_source: 'explicit',
              rules_fired: [],
              classifier_output: null,
              assignment_model: 'gpt-4o',
              capability_veto: false,
              veto_reason: null,
              qualified_candidates: [],
              escalated: false,
            },
            meta: { organization_id: 1, persisted: false, timestamp: '2026-01-01T00:00:00Z' },
          },
        });
      }
      return Promise.reject(new Error(`unexpected POST ${url}`));
    });

    const { container } = render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Sample Prompt')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText('Sample Prompt'), {
      target: { value: 'Write a bubble sort in Python' },
    });
    fireEvent.change(screen.getByLabelText('Tool Type (optional)'), { target: { value: 'code' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run Test' }));

    await waitFor(() => {
      expect(container.querySelector('.test-result')).not.toBeNull();
    });

    expect(axios.post).toHaveBeenCalledWith('/api/v1/routing/dry-run/', {
      prompt: 'Write a bubble sort in Python',
      tool_type: 'code',
    });

    const resultText = container.querySelector('.test-result').textContent;
    expect(resultText).toContain('gpt-4o');
    expect(resultText).toContain('code');
    expect(resultText).toContain('explicit');
    // No confidence score is ever shown -- RoutingEngine doesn't produce one,
    // and none is fabricated for display (unlike the retired endpoint).
    expect(resultText.toLowerCase()).not.toContain('confidence');
  });

  it('omits tool_type from the request when left blank', async () => {
    axios.post.mockResolvedValue({
      data: {
        status: 'success',
        data: {
          model: 'gpt-4',
          fallback_chain: [],
          routed_from: null,
          tool_type: 'general',
          tool_type_source: 'classifier',
          rules_fired: [],
          classifier_output: null,
          assignment_model: null,
          capability_veto: false,
          veto_reason: 'no_assignment',
          qualified_candidates: [],
          escalated: false,
        },
        meta: { organization_id: 1, persisted: false, timestamp: '2026-01-01T00:00:00Z' },
      },
    });

    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Sample Prompt')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText('Sample Prompt'), { target: { value: 'hello there' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run Test' }));

    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith('/api/v1/routing/dry-run/', { prompt: 'hello there' });
    });
  });

  it('shows an error message when the dry run fails, e.g. the flag is off', async () => {
    axios.post.mockRejectedValue({ response: { data: { error: 'Feature not enabled' } } });

    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Sample Prompt')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText('Sample Prompt'), { target: { value: 'hi' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run Test' }));

    await waitFor(() => {
      expect(screen.getByText('Feature not enabled')).toBeInTheDocument();
    });
  });

  it('dismisses the test-run error alert when its close button is clicked', async () => {
    axios.post.mockRejectedValue({ response: { data: { error: 'Feature not enabled' } } });

    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Sample Prompt')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText('Sample Prompt'), { target: { value: 'hi' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run Test' }));

    await waitFor(() => {
      expect(screen.getByText('Feature not enabled')).toBeInTheDocument();
    });

    const errorAlert = screen.getByText('Feature not enabled').closest('.alert');
    fireEvent.click(errorAlert.querySelector('button'));
    expect(screen.queryByText('Feature not enabled')).not.toBeInTheDocument();
  });

  it('deletes routing policy and seeds default assignments', async () => {
    axios.delete.mockResolvedValue({ data: { status: 'deleted' } });
    axios.post.mockResolvedValue({ data: { seeded: 3 } });

    render(<Routing />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete Policy' })).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Delete Policy' }));
    await waitFor(() => {
      expect(axios.delete).toHaveBeenCalledWith('/api/v1/routing/policies/1');
      expect(screen.getByText('Routing policy deleted successfully')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Seed Default Assignments' }));
    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith('/api/v1/routing/assignments/seed');
      expect(screen.getByText('Default routing assignments seeded')).toBeInTheDocument();
    });
  });

  it('manages routing rules, model aliases, access policies, and decisions tabs', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/routing/policies/1') return Promise.resolve({ data: { data: { classifier_prompt: 'p' } } });
      if (url === '/api/v1/routing/assignments/') return Promise.resolve({ data: { data: [] } });
      if (url === '/api/v1/routing/rules/') return Promise.resolve({ data: { data: [{ id: 1, name: 'rule-1', priority: 10, enabled: true }] } });
      if (url === '/api/v1/routing/aliases/') return Promise.resolve({ data: { data: [{ id: 2, source_model: 'fast', target_model: 'gpt-4o-mini', enabled: true }] } });
      if (url === '/api/v1/routing/access-policies/') return Promise.resolve({ data: { data: [{ id: 3, scope_type: 'org', scope_ref: '42', model_pattern: 'gpt-4*', action: 'deny', enabled: true }] } });
      if (url === '/api/v1/routing/decisions/') return Promise.resolve({ data: { data: [{ request_id: 'req-123', selected_model: 'gpt-4o', tool_type: 'code' }] } });
      if (url === '/api/v1/routing/decisions/req-123') return Promise.resolve({ data: { data: { request_id: 'req-123', selected_model: 'gpt-4o' } } });
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    axios.post.mockResolvedValue({ data: { status: 'created' } });
    axios.put.mockResolvedValue({ data: { status: 'updated' } });
    axios.delete.mockResolvedValue({ data: { status: 'deleted' } });

    render(<Routing />);
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Routing Rules' })).toBeInTheDocument());

    // Rules tab
    fireEvent.click(screen.getByRole('tab', { name: 'Routing Rules' }));
    await waitFor(() => expect(screen.getByText(/rule-1/)).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Rule Name'), { target: { value: 'rule-2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add Rule' }));
    await waitFor(() => expect(axios.post).toHaveBeenCalledWith('/api/v1/routing/rules/', expect.objectContaining({ name: 'rule-2' })));
    fireEvent.click(screen.getByRole('button', { name: 'Disable' }));
    await waitFor(() => expect(axios.put).toHaveBeenCalledWith('/api/v1/routing/rules/1', { enabled: false }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete Rule' }));
    await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/routing/rules/1'));

    // Aliases tab
    fireEvent.click(screen.getByRole('tab', { name: 'Model Aliases' }));
    await waitFor(() => expect(screen.getByText(/fast → gpt-4o-mini/)).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Source Model'), { target: { value: 'smart' } });
    fireEvent.change(screen.getByLabelText('Target Model'), { target: { value: 'gpt-4o' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add Alias' }));
    await waitFor(() => expect(axios.post).toHaveBeenCalledWith('/api/v1/routing/aliases/', expect.objectContaining({ source_model: 'smart', target_model: 'gpt-4o' })));
    fireEvent.click(screen.getByRole('button', { name: 'Disable' }));
    await waitFor(() => expect(axios.put).toHaveBeenCalledWith('/api/v1/routing/aliases/2', { enabled: false }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete Alias' }));
    await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/routing/aliases/2'));

    // Access Policies tab
    fireEvent.click(screen.getByRole('tab', { name: 'Access Policies' }));
    await waitFor(() => expect(screen.getByText(/gpt-4\*/)).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Model Pattern'), { target: { value: 'claude-*' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add Access Policy' }));
    await waitFor(() => expect(axios.post).toHaveBeenCalledWith('/api/v1/routing/access-policies/', expect.objectContaining({ model_pattern: 'claude-*' })));
    fireEvent.click(screen.getByRole('button', { name: 'Disable' }));
    await waitFor(() => expect(axios.put).toHaveBeenCalledWith('/api/v1/routing/access-policies/3', { enabled: false }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete Access Policy' }));
    await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/routing/access-policies/3'));

    // Decisions tab
    fireEvent.click(screen.getByRole('tab', { name: 'Decisions' }));
    await waitFor(() => expect(screen.getByText(/req-123 — gpt-4o/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Lookup Trace' }));
    fireEvent.change(screen.getByLabelText('Request ID'), { target: { value: 'req-123' } });
    fireEvent.click(screen.getByRole('button', { name: 'Lookup Trace' }));
    await waitFor(() => expect(axios.get).toHaveBeenCalledWith('/api/v1/routing/decisions/req-123'));
    expect(screen.getByText(/"selected_model": "gpt-4o"/)).toBeInTheDocument();
  });

  it('surfaces errors for delete policy, seed assignments, and tab mutations', async () => {
    axios.delete.mockRejectedValue(new Error('del err'));
    axios.post.mockRejectedValue(new Error('post err'));
    axios.put.mockRejectedValue(new Error('put err'));

    render(<Routing />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete Policy' })).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Delete Policy' }));
    await waitFor(() => expect(screen.getByText('Failed to delete routing policy')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Seed Default Assignments' }));
    await waitFor(() => expect(screen.getByText('Failed to seed routing assignments')).toBeInTheDocument());

    axios.get.mockRejectedValue(new Error('tab fetch err'));
    fireEvent.click(screen.getByRole('tab', { name: 'Routing Rules' }));
    await waitFor(() => expect(screen.getByText('Failed to load rules')).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText('Rule Name'), { target: { value: 'bad-rule' } });
    fireEvent.change(screen.getByLabelText('Rule Priority'), { target: { value: '50' } });
    fireEvent.change(screen.getByLabelText('Rule Action JSON'), { target: { value: '{"model":"x"}' } });
    fireEvent.change(screen.getByLabelText('Rule Match JSON'), { target: { value: '{bad-json' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add Rule' }));
    await waitFor(() => expect(screen.getByText(/Expected property name|JSON/i)).toBeInTheDocument());

    fireEvent.click(screen.getByRole('tab', { name: 'Model Aliases' }));
    await waitFor(() => expect(screen.getByText('Failed to load aliases')).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Source Model'), { target: { value: 'a' } });
    fireEvent.change(screen.getByLabelText('Target Model'), { target: { value: 'b' } });
    fireEvent.change(screen.getByLabelText('Target Provider'), { target: { value: 'openai' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add Alias' }));
    await waitFor(() => expect(screen.getByText('Failed to create model alias')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('tab', { name: 'Access Policies' }));
    await waitFor(() => expect(screen.getByText('Failed to load access')).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Scope Type'), { target: { value: 'team' } });
    fireEvent.change(screen.getByLabelText('Scope Ref'), { target: { value: 'team-1' } });
    fireEvent.change(screen.getByLabelText('Policy Action'), { target: { value: 'allow' } });
    fireEvent.change(screen.getByLabelText('Model Pattern'), { target: { value: '*' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add Access Policy' }));
    await waitFor(() => expect(screen.getByText('Failed to create access policy')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('tab', { name: 'Decisions' }));
    await waitFor(() => expect(screen.getByText('Failed to load decisions')).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Request ID'), { target: { value: 'missing' } });
    fireEvent.click(screen.getByRole('button', { name: 'Lookup Trace' }));
    await waitFor(() => expect(screen.getByText('Trace not found')).toBeInTheDocument());
  });

  it('surfaces toggle and delete errors on rules, aliases, and access policies', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/routing/policies/1') return Promise.resolve({ data: { data: { classifier_prompt: 'p' } } });
      if (url === '/api/v1/routing/assignments/') return Promise.resolve({ data: { data: [] } });
      if (url === '/api/v1/routing/rules/') return Promise.resolve({ data: { data: [{ id: 1, name: 'r1', priority: 1, enabled: false }] } });
      if (url === '/api/v1/routing/aliases/') return Promise.resolve({ data: { data: [{ id: 2, source_model: 'a', target_model: 'b', enabled: false }] } });
      if (url === '/api/v1/routing/access-policies/') return Promise.resolve({ data: { data: [{ id: 3, scope_type: 'org', model_pattern: '*', action: 'deny', enabled: false }] } });
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    axios.put.mockRejectedValue(new Error('put err'));
    axios.delete.mockRejectedValue(new Error('del err'));

    render(<Routing />);
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Routing Rules' })).toBeInTheDocument());

    fireEvent.click(screen.getByRole('tab', { name: 'Routing Rules' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Enable' })).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Enable' }));
    await waitFor(() => expect(screen.getByText('Failed to update routing rule')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Delete Rule' }));
    await waitFor(() => expect(screen.getByText('Failed to delete routing rule')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('tab', { name: 'Model Aliases' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Enable' })).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Enable' }));
    await waitFor(() => expect(screen.getByText('Failed to update model alias')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Delete Alias' }));
    await waitFor(() => expect(screen.getByText('Failed to delete model alias')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('tab', { name: 'Access Policies' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Enable' })).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Enable' }));
    await waitFor(() => expect(screen.getByText('Failed to update access policy')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Delete Access Policy' }));
    await waitFor(() => expect(screen.getByText('Failed to delete access policy')).toBeInTheDocument());
  });
});
