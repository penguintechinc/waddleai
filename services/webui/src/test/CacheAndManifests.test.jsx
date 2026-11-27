import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import CacheAndManifests from '../pages/CacheAndManifests';
import { getCacheConfig } from '../api/adminApi';

vi.mock('../pages/CacheAndManifests.css', () => ({}));
vi.mock('axios');
import axios from 'axios';

describe('CacheAndManifests page', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    window.confirm = vi.fn(() => true);
    axios.get.mockResolvedValue({
      data: {
        status: 'success',
        data: [
          {
            id: 1,
            scope_type: 'org',
            scope_ref: '1',
            exact_enabled: true,
            semantic_enabled: false,
            semantic_threshold: 0.95,
            ttl_seconds: 86400,
            max_entry_kb: 256,
            anthropic_cache_control: true,
          },
          {
            id: 2,
            scope_type: 'global',
            scope_ref: null,
            exact_enabled: false,
            semantic_enabled: true,
            semantic_threshold: 0.9,
            ttl_seconds: 3600,
            max_entry_kb: 128,
            anthropic_cache_control: false,
          },
        ],
      },
    });
    axios.post.mockImplementation((url, body) => {
      if (url === '/api/v1/manifests/apply' && body.dry_run) {
        return Promise.resolve({
          data: {
            plan_id: 'plan-123',
            manifest_digest: 'sha256:abc',
            items: [{ resource_type: 'provider', name: 'openai', action: 'create' }],
            dry_run: true,
          },
        });
      }
      if (url === '/api/v1/manifests/apply' && !body.dry_run) {
        return Promise.resolve({
          data: {
            plan_id: 'plan-123',
            items: [{ resource_type: 'provider', name: 'openai', action: 'create' }],
            dry_run: false,
          },
        });
      }
      return Promise.resolve({ data: { status: 'success', data: { id: 3 } } });
    });
    axios.put.mockResolvedValue({ data: { status: 'success', data: { id: 1 } } });
    axios.delete.mockResolvedValue({ data: { status: 'success', data: { id: 1, deleted: true } } });
  });

  it('lists, creates, updates, cancels, and deletes cache configs', async () => {
    render(<CacheAndManifests />);
    await waitFor(() => expect(screen.getByText('org:1')).toBeInTheDocument());
    expect(screen.getByRole('cell', { name: 'global' })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Scope Ref'), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText('TTL (seconds)'), { target: { value: '7200' } });
    fireEvent.change(screen.getByLabelText('Max Entry (KB)'), { target: { value: '512' } });
    fireEvent.change(screen.getByLabelText('Semantic Threshold'), { target: { value: '0.88' } });
    fireEvent.click(screen.getByLabelText('Exact Cache'));
    fireEvent.click(screen.getByLabelText('Semantic Cache'));
    fireEvent.click(screen.getByLabelText('Anthropic Cache Control'));
    fireEvent.click(screen.getByRole('button', { name: 'Create Cache Config' }));
    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith(
        '/api/v1/cache-configs',
        expect.objectContaining({ scope_type: 'org', scope_ref: '2', ttl_seconds: 7200, max_entry_kb: 512 })
      );
    });

    fireEvent.click(screen.getAllByText('Edit')[0]);
    fireEvent.change(screen.getAllByLabelText('TTL (seconds)')[1], { target: { value: '1200' } });
    fireEvent.change(screen.getAllByLabelText('Max Entry (KB)')[1], { target: { value: '1024' } });
    fireEvent.change(screen.getAllByLabelText('Semantic Threshold')[1], { target: { value: '0.92' } });
    fireEvent.click(screen.getByText('Cancel'));
    expect(screen.queryByText('Edit Cache Config #1')).toBeNull();

    fireEvent.click(screen.getAllByText('Edit')[0]);
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(axios.put).toHaveBeenCalledWith('/api/v1/cache-configs/1', expect.any(Object)));

    fireEvent.click(screen.getAllByText('Delete')[0]);
    await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/cache-configs/1'));

    window.confirm = vi.fn(() => false);
    fireEvent.click(screen.getAllByText('Delete')[0]);
    expect(axios.delete).toHaveBeenCalledTimes(1);

    // Global scope create + create/update/delete error paths
    fireEvent.change(screen.getByLabelText('Scope Type'), { target: { value: 'global' } });
    axios.post.mockRejectedValueOnce({ response: { data: { error: 'Create cache error' } } });
    fireEvent.click(screen.getByRole('button', { name: 'Create Cache Config' }));
    await waitFor(() => expect(screen.getByText('Create cache error')).toBeInTheDocument());

    axios.put.mockRejectedValueOnce({ response: { data: { error: 'Update cache error' } } });
    fireEvent.click(screen.getAllByText('Edit')[0]);
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(screen.getByText('Update cache error')).toBeInTheDocument());

    window.confirm = vi.fn(() => true);
    axios.delete.mockRejectedValueOnce({ response: { data: { error: 'Delete cache error' } } });
    fireEvent.click(screen.getAllByText('Delete')[0]);
    await waitFor(() => expect(screen.getByText('Delete cache error')).toBeInTheDocument());
  });

  it('previews and applies a DAC manifest and handles errors', async () => {
    render(<CacheAndManifests />);
    await waitFor(() => expect(screen.getByText('DAC Manifests')).toBeInTheDocument());
    fireEvent.click(screen.getByText('DAC Manifests'));

    fireEvent.change(screen.getByPlaceholderText(/Paste DAC v1alpha1/i), {
      target: { value: 'apiVersion: waddleai.penguintech.io/v1alpha1' },
    });
    fireEvent.click(screen.getByText('Preview Plan (Dry Run)'));
    await waitFor(() => expect(screen.getByText('Plan Preview: plan-123')).toBeInTheDocument());

    axios.post.mockRejectedValueOnce({ response: { data: { error: 'Apply failed' } } });
    fireEvent.click(screen.getByText('Confirm Apply'));
    await waitFor(() => expect(screen.getByText('Apply failed')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Confirm Apply'));
    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith('/api/v1/manifests/apply', {
        manifest: 'apiVersion: waddleai.penguintech.io/v1alpha1',
        dry_run: false,
        plan_id: 'plan-123',
      });
    });
    expect(screen.getByText('Applied Manifest Result')).toBeInTheDocument();

    axios.post.mockRejectedValueOnce({ response: { data: { error: 'Invalid manifest' } } });
    fireEvent.click(screen.getByText('Preview Plan (Dry Run)'));
    await waitFor(() => expect(screen.getByText('Invalid manifest')).toBeInTheDocument());
  });

  it('handles cache config errors, empty state, and getCacheConfig helper', async () => {
    axios.get.mockRejectedValueOnce(new Error('Network down'));
    render(<CacheAndManifests />);
    await waitFor(() => expect(screen.getByText('Network down')).toBeInTheDocument());

    axios.get.mockResolvedValueOnce({ data: { status: 'success', data: [] } });
    render(<CacheAndManifests />);
    await waitFor(() => expect(screen.getByText('No cache configs found')).toBeInTheDocument());

    axios.get.mockResolvedValueOnce({ data: { status: 'success', data: { id: 9, scope_type: 'global' } } });
    const single = await getCacheConfig(9);
    expect(single.id).toBe(9);
  });
});
