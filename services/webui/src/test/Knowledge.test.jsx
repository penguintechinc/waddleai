import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import Knowledge from '../pages/Knowledge';

vi.mock('../pages/Knowledge.css', () => ({}));
vi.mock('axios');
import axios from 'axios';

const longContent = 'A'.repeat(120);

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
              { id: 2, source: null, content: longContent, provenance: null, created_at: null },
              { id: 3, source: 'empty.md', content: '', provenance: null, created_at: null },
            ],
          },
        });
      }
      if (url === '/api/v1/knowledge/1') {
        return Promise.resolve({
          data: { id: 1, source: 'runbook.md', content: 'Restart service via kubectl', provenance: { source_filename: 'runbook.md' } },
        });
      }
      if (url === '/api/v1/knowledge/2') {
        return Promise.resolve({
          data: { id: 2, source: null, content: longContent, provenance: null },
        });
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    axios.post.mockResolvedValue({ data: { status: 'created', document_ids: [3], chunks: 1 } });
    axios.delete.mockResolvedValue({ data: { status: 'deleted', id: 1 } });
  });

  it('lists, views, uploads, and deletes knowledge documents', async () => {
    render(<Knowledge />);
    await waitFor(() => expect(screen.getByText('runbook.md')).toBeInTheDocument());
    expect(screen.getByText('upload')).toBeInTheDocument();
    expect(screen.getByText(`${'A'.repeat(100)}...`)).toBeInTheDocument();

    fireEvent.click(screen.getAllByText('View')[0]);
    await waitFor(() => expect(screen.getByText('Document #1: runbook.md')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Close'));

    fireEvent.click(screen.getAllByText('View')[1]);
    await waitFor(() => expect(screen.getByText('Document #2: upload')).toBeInTheDocument());
    // Delete doc 1 while doc 2 is open, then delete doc 2 while doc 2 is open
    fireEvent.click(screen.getAllByText('Delete')[0]);
    await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/knowledge/1'));
    fireEvent.click(screen.getAllByText('Delete')[1]);
    await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/knowledge/2'));

    const file = new File(['hello'], 'guide.md', { type: 'text/markdown' });
    const input = screen.getByLabelText(/Upload Document/i);
    fireEvent.change(input, { target: { files: [] } });
    fireEvent.change(input, { target: { files: [file] } });
    fireEvent.click(screen.getByText('Upload'));
    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith('/api/v1/knowledge', expect.any(FormData), expect.any(Object));
    });

    // Upload returning no chunks count (covers res?.chunks ?? 1)
    axios.post.mockResolvedValueOnce({ data: { status: 'created', document_ids: [4] } });
    fireEvent.change(input, { target: { files: [file] } });
    fireEvent.click(screen.getByText('Upload'));
    await waitFor(() => expect(screen.getByText('Uploaded 1 chunk(s)')).toBeInTheDocument());

    window.confirm = vi.fn(() => false);
    fireEvent.click(screen.getAllByText('Delete')[0]);
    expect(axios.delete).toHaveBeenCalledTimes(2);
  });

  it('handles fetch, upload, view, and delete errors cleanly', async () => {
    axios.get.mockRejectedValueOnce({ response: { data: { error: 'List failed' } } });
    render(<Knowledge />);
    await waitFor(() => expect(screen.getByText('List failed')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Upload'));
    expect(screen.getByText('Please select a file (.pdf, .md, .markdown, .txt)')).toBeInTheDocument();

    const file = new File(['bad'], 'bad.md', { type: 'text/markdown' });
    fireEvent.change(screen.getByLabelText(/Upload Document/i), { target: { files: [file] } });
    axios.post.mockRejectedValueOnce({ response: { data: { error: 'Quarantined' } } });
    fireEvent.click(screen.getByText('Upload'));
    await waitFor(() => expect(screen.getByText('Quarantined')).toBeInTheDocument());
  });

  it('renders empty state when no documents exist and handles view/delete errors', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/knowledge') {
        return Promise.resolve({
          data: { documents: [{ id: 1, source: 'a.md', content: 'short', created_at: '2026-11-27' }] },
        });
      }
      return Promise.reject({ response: { data: { error: 'Doc not found' } } });
    });
    render(<Knowledge />);
    await waitFor(() => expect(screen.getByText('a.md')).toBeInTheDocument());

    fireEvent.click(screen.getByText('View'));
    await waitFor(() => expect(screen.getByText('Doc not found')).toBeInTheDocument());

    axios.delete.mockRejectedValueOnce({ response: { data: { error: 'Delete failed' } } });
    fireEvent.click(screen.getByText('Delete'));
    await waitFor(() => expect(screen.getByText('Delete failed')).toBeInTheDocument());

    axios.get.mockResolvedValueOnce({ data: { documents: [] } });
    render(<Knowledge />);
    await waitFor(() => expect(screen.getByText('No knowledge documents uploaded')).toBeInTheDocument());
  });
});
