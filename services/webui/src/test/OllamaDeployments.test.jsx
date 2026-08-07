import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import OllamaDeployments from '../pages/OllamaDeployments';

// Mock CSS import
vi.mock('../pages/OllamaDeployments.css', () => ({}));

// Mock axios
vi.mock('axios');
import axios from 'axios';

const mockDeployments = [
  {
    id: 1,
    name: 'ollama-node-1',
    endpoint_url: 'http://ollama-node-1:11434',
    deployment_type: 'docker',
    status: 'running',
    health_status: 'healthy',
    gpu_config: { gpu_count: 2 },
    models: [
      { id: 1, model_name: 'llama3.2', model_tag: 'latest' },
      { id: 2, model_name: 'mistral', model_tag: '7b' },
    ],
  },
  {
    id: 2,
    name: 'ollama-node-2',
    endpoint_url: 'http://ollama-node-2:11434',
    deployment_type: 'kubernetes',
    status: 'stopped',
    health_status: 'unknown',
    gpu_config: { gpu_count: 0 },
    models: [],
  },
];

describe('OllamaDeployments', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    axios.get.mockResolvedValue({ data: { deployments: mockDeployments } });
  });

  it('shows loading state initially', async () => {
    axios.get.mockReturnValue(new Promise(() => {}));
    render(<OllamaDeployments />);
    expect(screen.getByText('Loading deployments...')).toBeInTheDocument();
  });

  it('renders page header', async () => {
    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByText('Ollama Deployments')).toBeInTheDocument();
    });
  });

  it('renders deployment cards after data loads', async () => {
    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByText('ollama-node-1')).toBeInTheDocument();
      expect(screen.getByText('ollama-node-2')).toBeInTheDocument();
    });
  });

  it('shows "+ New Deployment" button', async () => {
    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ New Deployment' })).toBeInTheDocument();
    });
  });

  it('shows empty state when no deployments exist', async () => {
    axios.get.mockResolvedValue({ data: { deployments: [] } });
    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByText('No Ollama deployments configured')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Create First Deployment' })).toBeInTheDocument();
    });
  });

  it('shows error when fetch fails', async () => {
    axios.get.mockRejectedValue({
      response: { data: { error: 'Connection refused' } },
    });
    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByText('Connection refused')).toBeInTheDocument();
    });
  });

  it('shows generic error when no response error field', async () => {
    axios.get.mockRejectedValue(new Error('Network error'));
    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByText('Failed to fetch deployments')).toBeInTheDocument();
    });
  });

  it('dismisses error alert when close button clicked', async () => {
    axios.get.mockRejectedValue(new Error('error'));
    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByText('Failed to fetch deployments')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: '×' }));
    expect(screen.queryByText('Failed to fetch deployments')).not.toBeInTheDocument();
  });

  it('opens create form when "+ New Deployment" clicked', async () => {
    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ New Deployment' })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: '+ New Deployment' }));
    expect(screen.getByText('Create Ollama Deployment')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('ollama-node-1')).toBeInTheDocument();
  });

  it('opens create form from empty state button', async () => {
    axios.get.mockResolvedValue({ data: { deployments: [] } });
    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Create First Deployment' })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create First Deployment' }));
    expect(screen.getByText('Create Ollama Deployment')).toBeInTheDocument();
  });

  it('closes create form when Cancel clicked', async () => {
    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ New Deployment' })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: '+ New Deployment' }));
    expect(screen.getByText('Create Ollama Deployment')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByText('Create Ollama Deployment')).not.toBeInTheDocument();
  });

  it('submits create form and refreshes deployments', async () => {
    axios.post.mockResolvedValue({ data: { id: 3 } });
    axios.get
      .mockResolvedValueOnce({ data: { deployments: mockDeployments } })
      .mockResolvedValueOnce({ data: { deployments: mockDeployments } });

    const user = userEvent.setup();
    render(<OllamaDeployments />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ New Deployment' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ New Deployment' }));

    await user.type(screen.getByPlaceholderText('ollama-node-1'), 'new-node');
    await user.type(screen.getByPlaceholderText('http://ollama-node-1:11434'), 'http://new-node:11434');

    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith('/api/v1/ollama/deployments', expect.objectContaining({
        name: 'new-node',
        endpoint_url: 'http://new-node:11434',
      }));
    });
  });

  it('shows error when create deployment fails', async () => {
    axios.post.mockRejectedValue({
      response: { data: { error: 'Name already taken' } },
    });

    const user = userEvent.setup();
    render(<OllamaDeployments />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ New Deployment' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ New Deployment' }));
    await user.type(screen.getByPlaceholderText('ollama-node-1'), 'dup');
    await user.type(screen.getByPlaceholderText('http://ollama-node-1:11434'), 'http://dup:11434');
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(screen.getByText('Name already taken')).toBeInTheDocument();
    });
  });

  it('renders models for a deployment', async () => {
    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByText('llama3.2:latest')).toBeInTheDocument();
      expect(screen.getByText('mistral:7b')).toBeInTheDocument();
    });
  });

  it('shows "No models pulled" when deployment has no models', async () => {
    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByText('No models pulled')).toBeInTheDocument();
    });
  });

  it('deletes deployment after confirmation', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    axios.delete.mockResolvedValue({ data: {} });
    axios.get
      .mockResolvedValueOnce({ data: { deployments: mockDeployments } })
      .mockResolvedValueOnce({ data: { deployments: [mockDeployments[1]] } });

    render(<OllamaDeployments />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);

    await waitFor(() => {
      expect(axios.delete).toHaveBeenCalledWith('/api/v1/ollama/deployments/1');
    });

    confirmSpy.mockRestore();
  });

  it('does not delete when confirmation is cancelled', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<OllamaDeployments />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);

    expect(axios.delete).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('shows error when delete deployment fails', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    axios.delete.mockRejectedValue({
      response: { data: { error: 'Cannot delete running deployment' } },
    });

    render(<OllamaDeployments />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);

    await waitFor(() => {
      expect(screen.getByText('Cannot delete running deployment')).toBeInTheDocument();
    });

    confirmSpy.mockRestore();
  });

  it('pulls a model when prompt returns model name', async () => {
    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue('llama3.2');
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {});
    axios.post.mockResolvedValue({ data: {} });
    axios.get
      .mockResolvedValueOnce({ data: { deployments: mockDeployments } })
      .mockResolvedValueOnce({ data: { deployments: mockDeployments } });

    render(<OllamaDeployments />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Pull Model' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Pull Model' })[0]);

    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith('/api/v1/ollama/deployments/1/models/pull', {
        model: 'llama3.2',
      });
    });

    promptSpy.mockRestore();
    alertSpy.mockRestore();
  });

  it('does not pull model when prompt is cancelled', async () => {
    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue(null);

    render(<OllamaDeployments />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Pull Model' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Pull Model' })[0]);
    expect(axios.post).not.toHaveBeenCalled();

    promptSpy.mockRestore();
  });

  it('shows error when pull model fails', async () => {
    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue('bad-model');
    axios.post.mockRejectedValue({
      response: { data: { error: 'Model not found' } },
    });

    render(<OllamaDeployments />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Pull Model' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Pull Model' })[0]);

    await waitFor(() => {
      expect(screen.getByText('Model not found')).toBeInTheDocument();
    });

    promptSpy.mockRestore();
  });

  it('renders deployment details: endpoint, type, gpu count, health', async () => {
    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByText('http://ollama-node-1:11434')).toBeInTheDocument();
      expect(screen.getByText('docker')).toBeInTheDocument();
      expect(screen.getByText('2')).toBeInTheDocument();
      expect(screen.getByText('healthy')).toBeInTheDocument();
    });
  });

  it('updates deployment type, GPU count, and auto-start toggle in create form', async () => {
    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ New Deployment' })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: '+ New Deployment' }));

    const typeSelect = screen.getByRole('combobox');
    fireEvent.change(typeSelect, { target: { value: 'kubernetes' } });
    expect(typeSelect).toHaveValue('kubernetes');

    const gpuInput = screen.getByDisplayValue('1');
    fireEvent.change(gpuInput, { target: { value: '4' } });
    expect(screen.getByDisplayValue('4')).toBeInTheDocument();

    const autoStartCheckbox = screen.getByRole('checkbox');
    expect(autoStartCheckbox).toBeChecked();
    fireEvent.click(autoStartCheckbox);
    expect(autoStartCheckbox).not.toBeChecked();
  });

  it('falls back to an empty deployments list when API response omits the field', async () => {
    axios.get.mockResolvedValue({ data: {} });
    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByText('No Ollama deployments configured')).toBeInTheDocument();
    });
  });

  it('shows generic error when create deployment fails without response error field', async () => {
    axios.post.mockRejectedValue(new Error('boom'));

    const user = userEvent.setup();
    render(<OllamaDeployments />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ New Deployment' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ New Deployment' }));
    await user.type(screen.getByPlaceholderText('ollama-node-1'), 'x');
    await user.type(screen.getByPlaceholderText('http://ollama-node-1:11434'), 'http://x:11434');
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(screen.getByText('Failed to create deployment')).toBeInTheDocument();
    });
  });

  it('shows generic error when delete deployment fails without response error field', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    axios.delete.mockRejectedValue(new Error('boom'));

    render(<OllamaDeployments />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);

    await waitFor(() => {
      expect(screen.getByText('Failed to delete deployment')).toBeInTheDocument();
    });

    confirmSpy.mockRestore();
  });

  it('shows generic error when pull model fails without response error field', async () => {
    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue('llama3.2');
    axios.post.mockRejectedValue(new Error('boom'));

    render(<OllamaDeployments />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Pull Model' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Pull Model' })[0]);

    await waitFor(() => {
      expect(screen.getByText('Failed to pull model')).toBeInTheDocument();
    });

    promptSpy.mockRestore();
  });

  it('renders "latest" as fallback model tag when a model has no tag', async () => {
    axios.get.mockResolvedValue({
      data: {
        deployments: [
          {
            id: 5,
            name: 'tagless-node',
            endpoint_url: 'http://tagless:11434',
            deployment_type: 'docker',
            status: 'running',
            health_status: 'healthy',
            gpu_config: { gpu_count: 1 },
            models: [{ id: 9, model_name: 'phi3' }],
          },
        ],
      },
    });

    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByText('phi3:latest')).toBeInTheDocument();
    });
  });

  it('shows "unknown" when health_status is absent', async () => {
    axios.get.mockResolvedValue({
      data: {
        deployments: [{
          id: 3,
          name: 'bare-node',
          endpoint_url: 'http://bare:11434',
          deployment_type: 'external',
          status: 'running',
          gpu_config: { gpu_count: 0 },
          models: [],
        }],
      },
    });

    render(<OllamaDeployments />);
    await waitFor(() => {
      expect(screen.getByText('unknown')).toBeInTheDocument();
    });
  });
});
