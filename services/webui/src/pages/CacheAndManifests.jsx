import { useState, useEffect } from 'react';
import {
  listCacheConfigs,
  createCacheConfig,
  updateCacheConfig,
  deleteCacheConfig,
  applyManifest,
} from '../api/adminApi';
import './CacheAndManifests.css';

const EMPTY_CACHE = {
  scope_type: 'org',
  scope_ref: '',
  exact_enabled: true,
  semantic_enabled: false,
  semantic_threshold: 0.95,
  ttl_seconds: 86400,
  max_entry_kb: 256,
  anthropic_cache_control: true,
};

function CacheAndManifests() {
  const [activeTab, setActiveTab] = useState('cache');
  const [configs, setConfigs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  const [cacheForm, setCacheForm] = useState(EMPTY_CACHE);
  const [editingConfig, setEditingConfig] = useState(null);

  // Manifests state
  const [manifestText, setManifestText] = useState('');
  const [planPreview, setPlanPreview] = useState(null);
  const [applyResult, setApplyResult] = useState(null);

  useEffect(() => {
    fetchConfigs();
  }, []);

  const flashSuccess = (msg) => {
    setSuccess(msg);
    setTimeout(() => setSuccess(null), 3000);
  };

  const fetchConfigs = async () => {
    try {
      setLoading(true);
      const data = await listCacheConfigs();
      setConfigs(Array.isArray(data) ? data : []);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleCreateConfig = async (e) => {
    e.preventDefault();
    try {
      await createCacheConfig({
        scope_type: cacheForm.scope_type,
        scope_ref: cacheForm.scope_type === 'global' ? null : cacheForm.scope_ref,
        exact_enabled: cacheForm.exact_enabled,
        semantic_enabled: cacheForm.semantic_enabled,
        semantic_threshold: parseFloat(cacheForm.semantic_threshold),
        ttl_seconds: parseInt(cacheForm.ttl_seconds, 10),
        max_entry_kb: parseInt(cacheForm.max_entry_kb, 10),
        anthropic_cache_control: cacheForm.anthropic_cache_control,
      });
      setCacheForm(EMPTY_CACHE);
      setError(null);
      flashSuccess('Cache config created');
      fetchConfigs();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleUpdateConfig = async (e) => {
    e.preventDefault();
    try {
      await updateCacheConfig(editingConfig.id, {
        exact_enabled: editingConfig.exact_enabled,
        semantic_enabled: editingConfig.semantic_enabled,
        semantic_threshold: parseFloat(editingConfig.semantic_threshold),
        ttl_seconds: parseInt(editingConfig.ttl_seconds, 10),
        max_entry_kb: parseInt(editingConfig.max_entry_kb, 10),
        anthropic_cache_control: editingConfig.anthropic_cache_control,
      });
      setEditingConfig(null);
      setError(null);
      flashSuccess('Cache config updated');
      fetchConfigs();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleDeleteConfig = async (id) => {
    if (!window.confirm('Delete this cache config?')) return;
    try {
      await deleteCacheConfig(id);
      setError(null);
      flashSuccess('Cache config deleted');
      fetchConfigs();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleDryRunPlan = async (e) => {
    e.preventDefault();
    try {
      const res = await applyManifest({ manifest: manifestText, dry_run: true });
      setPlanPreview(res);
      setApplyResult(null);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleConfirmApply = async () => {
    if (!planPreview?.plan_id) return;
    try {
      const res = await applyManifest({
        manifest: manifestText,
        dry_run: false,
        plan_id: planPreview.plan_id,
      });
      setApplyResult(res);
      setPlanPreview(null);
      setError(null);
      flashSuccess('Manifest applied');
    } catch (err) {
      setError(err.message);
    }
  };

  if (loading) {
    return <div className="admin-loading">Loading cache configs...</div>;
  }

  return (
    <div className="cache-manifests-page">
      <div className="page-header">
        <h1>Cache Configs &amp; Manifests</h1>
        <div className="tab-buttons">
          <button
            type="button"
            className={`tab-btn ${activeTab === 'cache' ? 'active' : ''}`}
            onClick={() => setActiveTab('cache')}
          >
            Cache Configs ({configs.length})
          </button>
          <button
            type="button"
            className={`tab-btn ${activeTab === 'manifests' ? 'active' : ''}`}
            onClick={() => setActiveTab('manifests')}
          >
            DAC Manifests
          </button>
        </div>
      </div>

      {error && <div className="alert alert-error">{error}</div>}
      {success && <div className="alert alert-success">{success}</div>}

      {activeTab === 'cache' && (
        <section>
          <form className="admin-form-card" onSubmit={handleCreateConfig}>
            <h2>Create Cache Config</h2>
            <div className="form-grid">
              <label>
                Scope Type
                <select
                  value={cacheForm.scope_type}
                  onChange={(e) => setCacheForm({ ...cacheForm, scope_type: e.target.value })}
                >
                  <option value="global">global</option>
                  <option value="org">org</option>
                  <option value="key">key</option>
                </select>
              </label>
              {cacheForm.scope_type !== 'global' && (
                <label>
                  Scope Ref
                  <input
                    type="text"
                    required
                    value={cacheForm.scope_ref}
                    onChange={(e) => setCacheForm({ ...cacheForm, scope_ref: e.target.value })}
                  />
                </label>
              )}
              <label>
                TTL (seconds)
                <input
                  type="number"
                  value={cacheForm.ttl_seconds}
                  onChange={(e) => setCacheForm({ ...cacheForm, ttl_seconds: e.target.value })}
                />
              </label>
              <label>
                Max Entry (KB)
                <input
                  type="number"
                  value={cacheForm.max_entry_kb}
                  onChange={(e) => setCacheForm({ ...cacheForm, max_entry_kb: e.target.value })}
                />
              </label>
              <label>
                Semantic Threshold
                <input
                  type="number"
                  step="0.01"
                  min="0.5"
                  max="1.0"
                  value={cacheForm.semantic_threshold}
                  onChange={(e) =>
                    setCacheForm({ ...cacheForm, semantic_threshold: e.target.value })
                  }
                />
              </label>
            </div>
            <div className="tier-toggles">
              <label>
                <input
                  type="checkbox"
                  checked={cacheForm.exact_enabled}
                  onChange={(e) => setCacheForm({ ...cacheForm, exact_enabled: e.target.checked })}
                />
                Exact Cache
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={cacheForm.semantic_enabled}
                  onChange={(e) =>
                    setCacheForm({ ...cacheForm, semantic_enabled: e.target.checked })
                  }
                />
                Semantic Cache
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={cacheForm.anthropic_cache_control}
                  onChange={(e) =>
                    setCacheForm({ ...cacheForm, anthropic_cache_control: e.target.checked })
                  }
                />
                Anthropic Cache Control
              </label>
            </div>
            <div className="form-actions">
              <button type="submit" className="btn-primary">Create Cache Config</button>
            </div>
          </form>

          {editingConfig && (
            <form className="admin-form-card" onSubmit={handleUpdateConfig}>
              <h2>Edit Cache Config #{editingConfig.id}</h2>
              <div className="form-grid">
                <label>
                  TTL (seconds)
                  <input
                    type="number"
                    value={editingConfig.ttl_seconds}
                    onChange={(e) =>
                      setEditingConfig({ ...editingConfig, ttl_seconds: e.target.value })
                    }
                  />
                </label>
                <label>
                  Max Entry (KB)
                  <input
                    type="number"
                    value={editingConfig.max_entry_kb}
                    onChange={(e) =>
                      setEditingConfig({ ...editingConfig, max_entry_kb: e.target.value })
                    }
                  />
                </label>
                <label>
                  Semantic Threshold
                  <input
                    type="number"
                    step="0.01"
                    min="0.5"
                    max="1.0"
                    value={editingConfig.semantic_threshold}
                    onChange={(e) =>
                      setEditingConfig({ ...editingConfig, semantic_threshold: e.target.value })
                    }
                  />
                </label>
              </div>
              <div className="form-actions">
                <button type="submit" className="btn-primary">Save</button>
                <button type="button" className="btn-secondary" onClick={() => setEditingConfig(null)}>
                  Cancel
                </button>
              </div>
            </form>
          )}

          <div className="admin-table-card">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Scope</th>
                  <th>Exact</th>
                  <th>Semantic</th>
                  <th>TTL (s)</th>
                  <th>Max KB</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {configs.length === 0 ? (
                  <tr>
                    <td colSpan="7" className="empty-cell">No cache configs found</td>
                  </tr>
                ) : (
                  configs.map((c) => (
                    <tr key={c.id}>
                      <td>{c.id}</td>
                      <td>{c.scope_type}{c.scope_ref ? `:${c.scope_ref}` : ''}</td>
                      <td>{c.exact_enabled ? 'Yes' : 'No'}</td>
                      <td>{c.semantic_enabled ? `Yes (${c.semantic_threshold})` : 'No'}</td>
                      <td>{c.ttl_seconds}</td>
                      <td>{c.max_entry_kb}</td>
                      <td className="actions-cell">
                        <button type="button" className="btn-sm" onClick={() => setEditingConfig({ ...c })}>
                          Edit
                        </button>
                        <button type="button" className="btn-sm btn-danger" onClick={() => handleDeleteConfig(c.id)}>
                          Delete
                        </button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {activeTab === 'manifests' && (
        <section>
          <form className="admin-form-card" onSubmit={handleDryRunPlan}>
            <h2>Configuration-as-Code Manifest (YAML / JSON)</h2>
            <textarea
              className="manifest-textarea"
              rows={10}
              placeholder="Paste DAC v1alpha1 YAML or JSON manifest..."
              required
              value={manifestText}
              onChange={(e) => setManifestText(e.target.value)}
            />
            <div className="form-actions">
              <button type="submit" className="btn-primary">Preview Plan (Dry Run)</button>
            </div>
          </form>

          {planPreview && (
            <div className="admin-form-card">
              <div className="modal-header">
                <h2>Plan Preview: {planPreview.plan_id}</h2>
                <button type="button" className="btn-primary" onClick={handleConfirmApply}>
                  Confirm Apply
                </button>
              </div>
              <p className="pool-meta">Digest: {planPreview.manifest_digest}</p>
              <pre className="json-preview">{JSON.stringify(planPreview.items || [], null, 2)}</pre>
            </div>
          )}

          {applyResult && (
            <div className="admin-form-card">
              <h2>Applied Manifest Result</h2>
              <pre className="json-preview">{JSON.stringify(applyResult, null, 2)}</pre>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

export default CacheAndManifests;
