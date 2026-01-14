import { useState, useEffect } from 'react';
import axios from 'axios';
import './VirtualKeys.css';

function VirtualKeys() {
  const [keys, setKeys] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [showCreatedKey, setShowCreatedKey] = useState(false);
  const [createdKeyValue, setCreatedKeyValue] = useState('');
  const [newKey, setNewKey] = useState({
    name: '',
    allowed_models: '',
    allowed_providers: '',
    rate_limit_rpm: 60,
    rate_limit_tpm: 10000,
    budget_limit: 100.0,
    expires_at: ''
  });

  useEffect(() => {
    fetchKeys();
  }, []);

  const fetchKeys = async () => {
    try {
      setLoading(true);
      const response = await axios.get('/api/v1/keys');
      setKeys(response.data.keys || []);
      setError(null);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to fetch virtual keys');
    } finally {
      setLoading(false);
    }
  };

  const handleCreateKey = async (e) => {
    e.preventDefault();
    try {
      const payload = {
        ...newKey,
        allowed_models: newKey.allowed_models ? newKey.allowed_models.split(',').map(m => m.trim()) : [],
        allowed_providers: newKey.allowed_providers ? newKey.allowed_providers.split(',').map(p => p.trim()) : [],
        rate_limit_rpm: parseInt(newKey.rate_limit_rpm),
        rate_limit_tpm: parseInt(newKey.rate_limit_tpm),
        budget_limit: parseFloat(newKey.budget_limit)
      };

      const response = await axios.post('/api/v1/keys', payload);
      setCreatedKeyValue(response.data.key);
      setShowCreatedKey(true);
      setShowCreateForm(false);
      setNewKey({
        name: '',
        allowed_models: '',
        allowed_providers: '',
        rate_limit_rpm: 60,
        rate_limit_tpm: 10000,
        budget_limit: 100.0,
        expires_at: ''
      });
      fetchKeys();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to create virtual key');
    }
  };

  const handleRevokeKey = async (keyId) => {
    if (!window.confirm('Are you sure you want to revoke this key?')) {
      return;
    }
    try {
      await axios.delete(`/api/v1/keys/${keyId}`);
      setSuccess('Key revoked successfully');
      fetchKeys();
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to revoke key');
    }
  };

  const handleRotateKey = async (keyId) => {
    if (!window.confirm('Are you sure you want to rotate this key? The old key will be revoked.')) {
      return;
    }
    try {
      const response = await axios.post(`/api/v1/keys/${keyId}/rotate`);
      setCreatedKeyValue(response.data.new_key);
      setShowCreatedKey(true);
      fetchKeys();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to rotate key');
    }
  };

  const copyToClipboard = (text) => {
    navigator.clipboard.writeText(text).then(() => {
      setSuccess('Copied to clipboard!');
      setTimeout(() => setSuccess(null), 2000);
    }).catch(() => {
      setError('Failed to copy to clipboard');
    });
  };

  const maskKey = (key) => {
    if (!key) return '';
    const prefix = key.substring(0, 12);
    return `${prefix}...`;
  };

  if (loading) {
    return <div className="loading">Loading virtual keys...</div>;
  }

  return (
    <div className="virtual-keys">
      <div className="page-header">
        <h1>Virtual Keys</h1>
        <button className="btn-primary" onClick={() => setShowCreateForm(true)}>
          + Create New Key
        </button>
      </div>

      {error && (
        <div className="alert alert-error">
          <strong>Error:</strong> {error}
          <button onClick={() => setError(null)}>&times;</button>
        </div>
      )}

      {success && (
        <div className="alert alert-success">
          <strong>Success:</strong> {success}
          <button onClick={() => setSuccess(null)}>&times;</button>
        </div>
      )}

      {showCreateForm && (
        <div className="modal">
          <div className="modal-content">
            <h2>Create Virtual Key</h2>
            <form onSubmit={handleCreateKey}>
              <div className="form-group">
                <label>Key Name</label>
                <input
                  type="text"
                  value={newKey.name}
                  onChange={(e) => setNewKey({ ...newKey, name: e.target.value })}
                  placeholder="Production API Key"
                  required
                />
                <small>Friendly name to identify this key</small>
              </div>

              <div className="form-group">
                <label>Allowed Models</label>
                <input
                  type="text"
                  value={newKey.allowed_models}
                  onChange={(e) => setNewKey({ ...newKey, allowed_models: e.target.value })}
                  placeholder="gpt-4, claude-3-opus-20240229"
                />
                <small>Comma-separated list of allowed models (leave empty for all)</small>
              </div>

              <div className="form-group">
                <label>Allowed Providers</label>
                <input
                  type="text"
                  value={newKey.allowed_providers}
                  onChange={(e) => setNewKey({ ...newKey, allowed_providers: e.target.value })}
                  placeholder="openai, anthropic, ollama"
                />
                <small>Comma-separated list of allowed providers (leave empty for all)</small>
              </div>

              <div className="form-row">
                <div className="form-group">
                  <label>Rate Limit (RPM)</label>
                  <input
                    type="number"
                    min="1"
                    value={newKey.rate_limit_rpm}
                    onChange={(e) => setNewKey({ ...newKey, rate_limit_rpm: e.target.value })}
                    required
                  />
                  <small>Requests per minute</small>
                </div>

                <div className="form-group">
                  <label>Rate Limit (TPM)</label>
                  <input
                    type="number"
                    min="1"
                    value={newKey.rate_limit_tpm}
                    onChange={(e) => setNewKey({ ...newKey, rate_limit_tpm: e.target.value })}
                    required
                  />
                  <small>Tokens per minute</small>
                </div>
              </div>

              <div className="form-group">
                <label>Budget Limit (USD)</label>
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  value={newKey.budget_limit}
                  onChange={(e) => setNewKey({ ...newKey, budget_limit: e.target.value })}
                  required
                />
                <small>Maximum spending allowed for this key</small>
              </div>

              <div className="form-group">
                <label>Expiration Date (Optional)</label>
                <input
                  type="datetime-local"
                  value={newKey.expires_at}
                  onChange={(e) => setNewKey({ ...newKey, expires_at: e.target.value })}
                />
                <small>Leave empty for no expiration</small>
              </div>

              <div className="form-actions">
                <button type="submit" className="btn-primary">Create Key</button>
                <button type="button" className="btn-secondary" onClick={() => setShowCreateForm(false)}>
                  Cancel
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {showCreatedKey && (
        <div className="modal">
          <div className="modal-content key-created-modal">
            <h2>Key Created Successfully</h2>
            <p>Please copy and save this key now. You won't be able to see it again.</p>
            <div className="key-display-large">
              <div className="key-value">{createdKeyValue}</div>
              <button className="btn-primary" onClick={() => copyToClipboard(createdKeyValue)}>
                Copy to Clipboard
              </button>
            </div>
            <p className="warning-text">This key will not be shown again!</p>
            <div className="form-actions">
              <button className="btn-secondary" onClick={() => setShowCreatedKey(false)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {keys.length === 0 ? (
        <div className="empty-state">
          <p>No virtual keys created yet</p>
          <button className="btn-primary" onClick={() => setShowCreateForm(true)}>
            Create First Key
          </button>
        </div>
      ) : (
        <div className="keys-table">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Key</th>
                <th>Models</th>
                <th>Providers</th>
                <th>Rate Limits</th>
                <th>Budget</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {keys.map((key) => (
                <tr key={key.id}>
                  <td>{key.name}</td>
                  <td>
                    <div className="key-display">
                      <span className="key-text">{maskKey(key.key_prefix)}</span>
                      <button
                        className="copy-btn"
                        onClick={() => copyToClipboard(key.key_prefix)}
                        title="Copy key prefix"
                      >
                        📋
                      </button>
                    </div>
                  </td>
                  <td>
                    {key.allowed_models && key.allowed_models.length > 0 ? (
                      <div className="tag-list">
                        {key.allowed_models.slice(0, 2).map((model, idx) => (
                          <span key={idx} className="tag">{model}</span>
                        ))}
                        {key.allowed_models.length > 2 && (
                          <span className="tag">+{key.allowed_models.length - 2}</span>
                        )}
                      </div>
                    ) : (
                      <span className="limit-text">All models</span>
                    )}
                  </td>
                  <td>
                    {key.allowed_providers && key.allowed_providers.length > 0 ? (
                      <div className="tag-list">
                        {key.allowed_providers.map((provider, idx) => (
                          <span key={idx} className="tag">{provider}</span>
                        ))}
                      </div>
                    ) : (
                      <span className="limit-text">All providers</span>
                    )}
                  </td>
                  <td>
                    <span className="limit-text">
                      {key.rate_limit_rpm} RPM<br />
                      {key.rate_limit_tpm} TPM
                    </span>
                  </td>
                  <td>
                    <span className="limit-text">
                      ${key.budget_used?.toFixed(2) || '0.00'} / ${key.budget_limit?.toFixed(2) || '0.00'}
                    </span>
                  </td>
                  <td>
                    <span className={`status-badge ${key.is_active ? 'active' : 'inactive'}`}>
                      {key.is_active ? 'Active' : 'Revoked'}
                    </span>
                  </td>
                  <td>
                    <div className="action-buttons">
                      <button
                        className="btn-icon"
                        onClick={() => handleRotateKey(key.id)}
                        title="Rotate key"
                        disabled={!key.is_active}
                      >
                        🔄
                      </button>
                      <button
                        className="btn-icon danger"
                        onClick={() => handleRevokeKey(key.id)}
                        title="Revoke key"
                        disabled={!key.is_active}
                      >
                        🗑️
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default VirtualKeys;
