import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import Knowledge from '../pages/Knowledge';

vi.mock('../pages/Knowledge.css', () => ({}));
vi.mock('axios');
import axios from 'axios';

describe('Knowledge page', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    window.confirm = vi.fn(() => true);
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/knowledge') {
        return Promise.resolve({
          data: {
            documents: [
              { id: 1, source: 'runbook.md', content: 'Restart service via kubectl', provenance: { source_filename: 'runbook.md' }, created_at: '2026-11-27T00:00:00' },
            ],
          },
        });
      }
      if (url === '/api/v1/knowledge/1') {
        return Promise.resolve({
          data: { id: 1, source: 'runbook.md', content: 'Restart service via kubectl', provenance: { source_filename: 'runbook.md' } },
        });
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    axios.post.mockResolvedValue({ data: { status: 'created', document_ids: [2], chunks: 1 } });
    axios.delete.mockResolvedValue({ data: { status: 'deleted', id: 1 } });
  });

  it('lists, views, uploads, and deletes knowledge documents', async () => {
    render(<Knowledge />);
    await waitFor(() => expect(screen.getByText('runbook.md')).toBeInTheDocument());

    fireEvent.click(screen.getByText('View'));
    await waitFor(() => expect(screen.getByText('Document #1: runbook.md')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Close'));

    const file = new File(['hello'], 'guide.md', { type: 'text/markdown' });
    const input = screen.getByLabelText(/Upload Document/i);
    fireEvent.change(input, { target: { files: [file] } });
    fireEvent.click(screen.getByText('Upload'));
    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith('/api/v1/knowledge', expect.any(FormData), expect.any(Object));
    });

    fireEvent.click(screen.getByText('Delete'));
    await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/knowledge/1'));
  });

  it('handles validation and API errors cleanly', async () => {
    render(<Knowledge />);
    await waitFor(() => expect(screen.getByText('Upload')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Upload'));
    expect(screen.getByText('Please select a file (.pdf, .md, .markdown, .txt)')).toBeInTheDocument();

    axios.get.mockRejectedValueOnce({ response: { data: { error: 'Doc not found' } } });
    fireEvent.click(screen.getByText('View'));
    await waitFor(() => expect(screen.getByText('Doc not found')).toBeInTheDocument());

    axios.delete.mockRejectedValueOnce({ response: { data: { error: 'Delete failed' } } });
    fireEvent.click(screen.getByText('Delete'));
    await waitFor(() => expect(screen.getByText('Delete failed')).toBeInTheDocument());
  });

  it('renders empty state when no documents exist', async () => {
    axios.get.mockResolvedValueOnce({ data: { documents: [] } });
    render(<Knowledge />);
    await waitFor(() => expect(screen.getByText('No knowledge documents uploaded')).toBeInTheDocument());
  });
});
