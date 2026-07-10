import { render, screen, waitFor, fireEvent } from '@testing-library/react';
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
});
