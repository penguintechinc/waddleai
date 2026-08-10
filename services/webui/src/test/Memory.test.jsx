import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import Memory from '../pages/Memory';

// Mock CSS import
vi.mock('../pages/Memory.css', () => ({}));

// Mock axios
vi.mock('axios');
import axios from 'axios';

const mockMemoryConfig = { organization_id: 1, enabled: true, max_messages: 20, similarity_threshold: 0.7, configured: true };
const mockRagConfig = {
  organization_id: 1,
  enabled: false,
  collection: 'default',
  top_k: 5,
  similarity_threshold: 0.7,
  configured: true,
};
const mockEmbeddingConfig = {
  organization_id: 1,
  backend: 'ollama',
  model: 'nomic-embed-text',
  ollama_host: 'http://localhost:11434',
  dimensions: 768,
  configured: true,
};

describe('Memory page', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    axios.get.mockImplementation((path) => {
      if (path === '/api/v1/ailb/memory-config') return Promise.resolve({ data: mockMemoryConfig });
      if (path === '/api/v1/ailb/rag-config') return Promise.resolve({ data: mockRagConfig });
      if (path === '/api/v1/ailb/embedding-config') return Promise.resolve({ data: mockEmbeddingConfig });
      return Promise.reject(new Error(`unexpected path ${path}`));
    });
  });

  it('shows loading state initially', () => {
    axios.get.mockReturnValue(new Promise(() => {}));
    render(<Memory />);
    expect(screen.getByText('Loading memory configuration...')).toBeInTheDocument();
  });

  it('renders page header', async () => {
    render(<Memory />);
    await waitFor(() => {
      expect(screen.getByText('Memory & Retrieval Configuration')).toBeInTheDocument();
    });
  });

  it('loads and displays all three config sections', async () => {
    render(<Memory />);
    await waitFor(() => {
      expect(screen.getByText('Conversation Memory (mem0 / pgvector)')).toBeInTheDocument();
    });
    expect(screen.getByText('RAG Document Retrieval')).toBeInTheDocument();
    expect(screen.getByText('Embedding Backend')).toBeInTheDocument();
    expect(axios.get).toHaveBeenCalledWith('/api/v1/ailb/memory-config', { params: { organization_id: 1 } });
    expect(axios.get).toHaveBeenCalledWith('/api/v1/ailb/rag-config', { params: { organization_id: 1 } });
    expect(axios.get).toHaveBeenCalledWith('/api/v1/ailb/embedding-config', { params: { organization_id: 1 } });
  });

  it('refetches all configs when organization id changes', async () => {
    render(<Memory />);
    await waitFor(() => {
      expect(screen.getByLabelText('Organization ID')).toBeInTheDocument();
    });

    axios.get.mockClear();
    fireEvent.change(screen.getByLabelText('Organization ID'), { target: { value: '2' } });

    await waitFor(() => {
      expect(axios.get).toHaveBeenCalledWith('/api/v1/ailb/memory-config', { params: { organization_id: 2 } });
    });
  });

  it('shows error message when fetch fails', async () => {
    axios.get.mockRejectedValue({ response: { data: { error: 'Admin permission required' } } });
    render(<Memory />);
    await waitFor(() => {
      expect(screen.getByText('Admin permission required')).toBeInTheDocument();
    });
  });

  it('shows generic error when no response error field', async () => {
    axios.get.mockRejectedValue(new Error('Network error'));
    render(<Memory />);
    await waitFor(() => {
      expect(screen.getByText('Failed to fetch memory configuration')).toBeInTheDocument();
    });
  });

  it('dismisses error alert when close button clicked', async () => {
    axios.get.mockRejectedValue(new Error('error'));
    render(<Memory />);
    await waitFor(() => {
      expect(screen.getByText('Failed to fetch memory configuration')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: '×' }));
    expect(screen.queryByText('Failed to fetch memory configuration')).not.toBeInTheDocument();
  });

  it('saves memory configuration and shows success message', async () => {
    axios.post.mockResolvedValue({ data: { status: 'updated', organization_id: 1 } });
    render(<Memory />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Memory Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Memory Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('Memory configuration saved successfully')).toBeInTheDocument();
    });

    expect(axios.post).toHaveBeenCalledWith('/api/v1/ailb/memory-config', {
      organization_id: 1,
      enabled: true,
      max_messages: 20,
      similarity_threshold: 0.7,
      configured: true,
    });
  });

  it('saves RAG configuration and shows success message', async () => {
    axios.post.mockResolvedValue({ data: { status: 'updated', organization_id: 1 } });
    render(<Memory />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save RAG Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save RAG Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('RAG configuration saved successfully')).toBeInTheDocument();
    });
  });

  it('saves embedding configuration and shows success message', async () => {
    axios.post.mockResolvedValue({ data: { status: 'updated', backend: 'ollama' } });
    render(<Memory />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Embedding Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Embedding Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('Embedding configuration saved successfully')).toBeInTheDocument();
    });
  });

  it('shows error when save fails', async () => {
    axios.post.mockRejectedValue({ response: { data: { error: 'backend must be one of: ollama, openai, anthropic' } } });
    render(<Memory />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Embedding Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Embedding Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('backend must be one of: ollama, openai, anthropic')).toBeInTheDocument();
    });
  });

  it('shows generic error text when save fails without response error field', async () => {
    axios.post.mockRejectedValue(new Error('Network error'));
    render(<Memory />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Memory Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Memory Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('Failed to save memory configuration')).toBeInTheDocument();
    });
  });

  it('toggles enabled checkboxes', async () => {
    render(<Memory />);
    await waitFor(() => {
      expect(screen.getByText('Conversation Memory (mem0 / pgvector)')).toBeInTheDocument();
    });

    const checkboxes = screen.getAllByRole('checkbox');
    expect(checkboxes[0]).toBeChecked();
    fireEvent.click(checkboxes[0]);
    expect(checkboxes[0]).not.toBeChecked();
  });

  it('falls back to organization id 1 when input is cleared to a falsy value', async () => {
    render(<Memory />);
    await waitFor(() => {
      expect(screen.getByLabelText('Organization ID')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByLabelText('Organization ID'), { target: { value: '0' } });
    expect(screen.getByLabelText('Organization ID')).toHaveValue(1);
  });

  it('updates memory max messages and similarity threshold fields', async () => {
    render(<Memory />);
    await waitFor(() => {
      expect(document.getElementById('memory-max-messages')).toBeInTheDocument();
    });

    fireEvent.change(document.getElementById('memory-max-messages'), { target: { value: '50' } });
    expect(document.getElementById('memory-max-messages')).toHaveValue(50);

    fireEvent.change(document.getElementById('memory-similarity'), { target: { value: '0.9' } });
    expect(document.getElementById('memory-similarity')).toHaveValue(0.9);
  });

  it('toggles rag enabled checkbox and updates rag collection, top k, and similarity fields', async () => {
    render(<Memory />);
    await waitFor(() => {
      expect(document.getElementById('rag-collection')).toBeInTheDocument();
    });

    const checkboxes = screen.getAllByRole('checkbox');
    expect(checkboxes[1]).not.toBeChecked();
    fireEvent.click(checkboxes[1]);
    expect(checkboxes[1]).toBeChecked();

    fireEvent.change(document.getElementById('rag-collection'), { target: { value: 'docs' } });
    expect(document.getElementById('rag-collection')).toHaveValue('docs');

    fireEvent.change(document.getElementById('rag-top-k'), { target: { value: '10' } });
    expect(document.getElementById('rag-top-k')).toHaveValue(10);

    fireEvent.change(document.getElementById('rag-similarity'), { target: { value: '0.8' } });
    expect(document.getElementById('rag-similarity')).toHaveValue(0.8);
  });

  it('updates embedding backend, model, host, and dimensions fields', async () => {
    render(<Memory />);
    await waitFor(() => {
      expect(document.getElementById('embedding-backend')).toBeInTheDocument();
    });

    fireEvent.change(document.getElementById('embedding-backend'), { target: { value: 'openai' } });
    expect(document.getElementById('embedding-backend')).toHaveValue('openai');

    fireEvent.change(document.getElementById('embedding-model'), {
      target: { value: 'text-embedding-3-small' },
    });
    expect(document.getElementById('embedding-model')).toHaveValue('text-embedding-3-small');

    fireEvent.change(document.getElementById('embedding-host'), {
      target: { value: 'http://localhost:9999' },
    });
    expect(document.getElementById('embedding-host')).toHaveValue('http://localhost:9999');

    fireEvent.change(document.getElementById('embedding-dimensions'), { target: { value: '1536' } });
    expect(document.getElementById('embedding-dimensions')).toHaveValue(1536);
  });

  it('dismisses success alert when close button clicked', async () => {
    axios.post.mockResolvedValue({ data: { status: 'updated' } });
    render(<Memory />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Memory Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Memory Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('Memory configuration saved successfully')).toBeInTheDocument();
    });

    const successAlert = screen.getByText('Memory configuration saved successfully').closest('.alert');
    fireEvent.click(successAlert.querySelector('button'));
    expect(screen.queryByText('Memory configuration saved successfully')).not.toBeInTheDocument();
  });

  it('clears success message automatically after 3 seconds', async () => {
    axios.post.mockResolvedValue({ data: { status: 'updated' } });
    render(<Memory />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Memory Configuration' })).toBeInTheDocument();
    });

    vi.useFakeTimers({ shouldAdvanceTime: false });
    fireEvent.click(screen.getByRole('button', { name: 'Save Memory Configuration' }));

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByText('Memory configuration saved successfully')).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(3000);
    });

    expect(screen.queryByText('Memory configuration saved successfully')).not.toBeInTheDocument();
    vi.useRealTimers();
  });
});
