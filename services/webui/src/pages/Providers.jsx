import { useState, useEffect } from 'react';
import axios from 'axios';
import './Providers.css';

const FALLBACK_PROVIDER_TYPES = [
  { type: 'openai', name: 'OpenAI' },
  { type: 'anthropic', name: 'Anthropic' },
  { type: 'ollama', name: 'Ollama' },
  { type: 'azure_openai', name: 'Azure OpenAI' },
  { type: 'gemini', name: 'Google Gemini' },
  { type: 'cohere', name: 'Cohere' },
  { type: 'custom', name: 'Custom' },
];

function errMsg(err, fallback) {
  return err?.response?.data?.error || fallback;
}

function Providers() {
  const [providers, setProviders] = useState([]);
  const [providerTypes, setProviderTypes] = useState(FALLBACK_PROVIDER_TYPES);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [editingProvider, setEditingProvider] = useState(null);
  const [modelsModal, setModelsModal] = useState(null);
  const [credsModal, setCredsModal] = useState(null);
  const [credForm, setCredForm] = useState({ label: '', api_key: '', weight: 100, enabled: true });
  const [formData, setFormData] = useState({
    name: '',
    provider_type: 'openai',
    endpoint_url: '',
    api_key: '',
    priority: 1,
    is_active: true
  });

  useEffect(() => {
    fetchProviders();
  }, []);

  const fetchProviders = async () => {
    try {
      setLoading(true);
      const [providersRes, typesRes] = await Promise.all([
        axios.get('/api/v1/providers'),
        axios.get('/api/v1/providers/types').catch(() => ({ data: { provider_types: [] } })),
      ]);
      setProviders(providersRes.data.providers || []);
      const fetchedTypes = typesRes.data?.provider_types;
      if (Array.isArray(fetchedTypes) && fetchedTypes.length > 0) {
        setProviderTypes(fetchedTypes);
      }
      setError(null);
    } catch (err) {
      setError(errMsg(err, 'Failed to fetch providers'));
    } finally {
      setLoading(false);
    }
  };

  const handleCreateProvider = async (e) => {
    e.preventDefault();
    try {
      await axios.post('/api/v1/providers', formData);
      setSuccess('Provider created successfully');
      setShowCreateForm(false);
      resetForm();
      fetchProviders();
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(errMsg(err, 'Failed to create provider'));
    }
  };

  const handleUpdateProvider = async (e) => {
    e.preventDefault();
    try {
      await axios.put(`/api/v1/providers/${editingProvider.id}`, formData);
      setSuccess('Provider updated successfully');
      setEditingProvider(null);
      resetForm();
      fetchProviders();
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(errMsg(err, 'Failed to update provider'));
    }
  };

  const handleDeleteProvider = async (providerId) => {
    if (!window.confirm('Are you sure you want to delete this provider?')) {
      return;
    }
    try {
      await axios.delete(`/api/v1/providers/${providerId}`);
      setSuccess('Provider deleted successfully');
      fetchProviders();
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(errMsg(err, 'Failed to delete provider'));
    }
  };

  const handleTestConnection = async (providerId) => {
    try {
      const response = await axios.post(`/api/v1/providers/${providerId}/test`);
      if (response.data.success) {
        setSuccess('Connection test successful!');
      } else {
        setError('Connection test failed');
      }
      setTimeout(() => {
        setSuccess(null);
        setError(null);
      }, 3000);
    } catch (err) {
      setError(errMsg(err, 'Connection test failed'));
    }
  };

  const handleViewModels = async (provider) => {
    try {
      const response = await axios.get(`/api/v1/providers/${provider.id}/models`);
      setModelsModal({
        provider,
        models: response.data?.models || [],
      });
    } catch (err) {
      setError(errMsg(err, 'Failed to fetch provider models'));
    }
  };

  const openCredentialsModal = async (provider) => {
    try {
      const response = await axios.get(`/api/v1/providers/${provider.id}/credentials`);
      setCredsModal({
        provider,
        credentials: response.data?.data || [],
      });
      setCredForm({ label: '', api_key: '', weight: 100, enabled: true });
    } catch (err) {
      setError(errMsg(err, 'Failed to fetch provider credentials'));
    }
  };

  const handleCreateCredential = async (e) => {
    e.preventDefault();
    if (!credsModal) return;
    try {
      await axios.post(`/api/v1/providers/${credsModal.provider.id}/credentials`, {
        label: credForm.label,
        api_key: credForm.api_key,
        weight: Number(credForm.weight) || 100,
        enabled: credForm.enabled,
      });
      setSuccess('Credential added successfully');
      await openCredentialsModal(credsModal.provider);
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(errMsg(err, 'Failed to add credential'));
    }
  };

  const handleToggleCredential = async (cred) => {
    if (!credsModal) return;
    try {
      await axios.patch(
        `/api/v1/providers/${credsModal.provider.id}/credentials/${cred.id}`,
        { enabled: !cred.enabled }
      );
      setSuccess('Credential updated successfully');
      await openCredentialsModal(credsModal.provider);
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(errMsg(err, 'Failed to update credential'));
    }
  };

  const handleDeleteCredential = async (credId) => {
    if (!credsModal) return;
    try {
      await axios.delete(`/api/v1/providers/${credsModal.provider.id}/credentials/${credId}`);
      setSuccess('Credential deleted successfully');
      await openCredentialsModal(credsModal.provider);
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(errMsg(err, 'Failed to delete credential'));
    }
  };

  const resetForm = () => {
    setFormData({
      name: '',
      provider_type: 'openai',
      endpoint_url: '',
      api_key: '',
      priority: 1,
      is_active: true
    });
  };

  const openEditForm = (provider) => {
    setEditingProvider(provider);
    setFormData({
      name: provider.name,
      provider_type: provider.provider_type,
      endpoint_url: provider.endpoint_url || '',
      api_key: '',
      priority: provider.priority || 1,
      is_active: provider.is_active !== false
    });
  };

  const getProviderIcon = (type) => {
    switch (type?.toLowerCase()) {
      case 'openai':
        return { className: 'openai', icon: '🤖' };
      case 'anthropic':
        return { className: 'anthropic', icon: '🧠' };
      case 'ollama':
        return { className: 'ollama', icon: '🦙' };
      default:
        return { className: 'generic', icon: '🔌' };
    }
  };

  if (loading) {
    return <div className="loading">Loading providers...</div>;
  }

  return (
    <div className="providers">
      <div className="page-header">
        <h1>AI Providers</h1>
        <div style={{ display: 'flex', gap: '1rem' }}>
          <button className="btn-primary" onClick={() => setShowCreateForm(true)}>
            + Add Provider
          </button>
        </div>
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

      {(showCreateForm || editingProvider) && (
        <div className="modal">
          <div className="modal-content">
            <h2>{editingProvider ? 'Edit Provider' : 'Add New Provider'}</h2>
            <form onSubmit={editingProvider ? handleUpdateProvider : handleCreateProvider}>
              <div className="form-group">
                <label>Provider Name</label>
                <input
                  type="text"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  placeholder="My OpenAI Provider"
                  required
                />
                <small>Friendly name for this provider</small>
              </div>

              <div className="form-group">
                <label>Provider Type</label>
                <select
                  value={formData.provider_type}
                  onChange={(e) => setFormData({ ...formData, provider_type: e.target.value })}
                  required
                >
                  {providerTypes.map((pt) => (
                    <option key={pt.type} value={pt.type}>
                      {pt.name}
                    </option>
                  ))}
                </select>
              </div>

              <div className="form-group">
                <label>Endpoint URL</label>
                <input
                  type="url"
                  value={formData.endpoint_url}
                  onChange={(e) => setFormData({ ...formData, endpoint_url: e.target.value })}
                  placeholder="https://api.openai.com/v1"
                />
                <small>Leave empty to use default endpoint</small>
              </div>

              <div className="form-group">
                <label>API Key</label>
                <input
                  type="password"
                  value={formData.api_key}
                  onChange={(e) => setFormData({ ...formData, api_key: e.target.value })}
                  placeholder={editingProvider ? '(leave empty to keep current)' : 'sk-...'}
                  required={!editingProvider}
                />
                <small>API key for authentication</small>
              </div>

              <div className="form-group">
                <label>Priority</label>
                <input
                  type="number"
                  min="1"
                  max="100"
                  value={formData.priority}
                  onChange={(e) => setFormData({ ...formData, priority: e.target.value })}
                  required
                />
                <small>Lower numbers have higher priority in load balancing</small>
              </div>

              <div className="form-group">
                <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <input
                    type="checkbox"
                    checked={formData.is_active}
                    onChange={(e) => setFormData({ ...formData, is_active: e.target.checked })}
                  />
                  Provider is active
                </label>
              </div>

              <div className="form-actions">
                <button type="submit" className="btn-primary">
                  {editingProvider ? 'Update Provider' : 'Add Provider'}
                </button>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => {
                    setShowCreateForm(false);
                    setEditingProvider(null);
                    resetForm();
                  }}
                >
                  Cancel
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {providers.length === 0 ? (
        <div className="empty-state">
          <p>No AI providers configured</p>
          <button className="btn-primary" onClick={() => setShowCreateForm(true)}>
            Add First Provider
          </button>
        </div>
      ) : (
        <div className="providers-grid">
          {providers.map((provider) => {
            const { className, icon } = getProviderIcon(provider.provider_type);
            return (
              <div key={provider.id} className="provider-card">
                <div className="provider-header">
                  <div className="provider-title">
                    <div className={`provider-icon ${className}`}>{icon}</div>
                    <h3 className="provider-name">{provider.name}</h3>
                  </div>
                  <span className={`status-badge ${provider.is_active ? 'active' : 'inactive'}`}>
                    {provider.is_active ? 'Active' : 'Inactive'}
                  </span>
                </div>

                <div className="provider-details">
                  <div className="detail-row">
                    <span className="label">Type:</span>
                    <span className="value">{provider.provider_type}</span>
                  </div>
                  <div className="detail-row">
                    <span className="label">Endpoint:</span>
                    <span className="value">
                      {provider.endpoint_url || `Default ${provider.provider_type}`}
                    </span>
                  </div>
                  <div className="detail-row">
                    <span className="label">Priority:</span>
                    <span className="value">{provider.priority || 1}</span>
                  </div>
                  <div className="detail-row">
                    <span className="label">Health:</span>
                    <span className={`value ${provider.health_status || 'unknown'}`}>
                      {provider.health_status || 'Unknown'}
                    </span>
                  </div>
                </div>

                <div className="provider-actions">
                  <button
                    className="btn-small btn-primary"
                    onClick={() => handleTestConnection(provider.id)}
                  >
                    Test
                  </button>
                  <button
                    className="btn-small btn-secondary"
                    onClick={() => handleViewModels(provider)}
                  >
                    Models
                  </button>
                  <button
                    className="btn-small btn-secondary"
                    onClick={() => openCredentialsModal(provider)}
                  >
                    Credentials
                  </button>
                  <button
                    className="btn-small btn-secondary"
                    onClick={() => openEditForm(provider)}
                  >
                    Edit
                  </button>
                  <button
                    className="btn-small btn-danger"
                    onClick={() => handleDeleteProvider(provider.id)}
                  >
                    Delete
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {modelsModal && (
        <div className="modal">
          <div className="modal-content">
            <h2>Models — {modelsModal.provider.name}</h2>
            {modelsModal.models.length === 0 ? (
              <p>No models available for this provider.</p>
            ) : (
              <ul>
                {modelsModal.models.map((m) => (
                  <li key={m}>{m}</li>
                ))}
              </ul>
            )}
            <div className="form-actions">
              <button type="button" className="btn-secondary" onClick={() => setModelsModal(null)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {credsModal && (
        <div className="modal">
          <div className="modal-content">
            <h2>Credentials — {credsModal.provider.name}</h2>
            {credsModal.credentials.length === 0 ? (
              <p>No credentials configured.</p>
            ) : (
              <ul className="credentials-list">
                {credsModal.credentials.map((cred) => (
                  <li key={cred.id} className="credential-item">
                    <span>
                      <strong>{cred.label}</strong> ({cred.api_key_masked}) — weight {cred.weight} —{' '}
                      {cred.enabled ? 'Enabled' : 'Disabled'}
                    </span>
                    <div style={{ display: 'flex', gap: '0.5rem' }}>
                      <button
                        type="button"
                        className="btn-small btn-secondary"
                        onClick={() => handleToggleCredential(cred)}
                      >
                        {cred.enabled ? 'Disable' : 'Enable'}
                      </button>
                      <button
                        type="button"
                        className="btn-small btn-danger"
                        onClick={() => handleDeleteCredential(cred.id)}
                      >
                        Remove
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}

            <form onSubmit={handleCreateCredential} style={{ marginTop: '1rem' }}>
              <h3>Add Credential</h3>
              <div className="form-group">
                <label htmlFor="cred-label">Label</label>
                <input
                  id="cred-label"
                  type="text"
                  value={credForm.label}
                  onChange={(e) => setCredForm({ ...credForm, label: e.target.value })}
                  required
                />
              </div>
              <div className="form-group">
                <label htmlFor="cred-api-key">API Key</label>
                <input
                  id="cred-api-key"
                  type="password"
                  value={credForm.api_key}
                  onChange={(e) => setCredForm({ ...credForm, api_key: e.target.value })}
                  required
                />
              </div>
              <div className="form-group">
                <label htmlFor="cred-weight">Weight</label>
                <input
                  id="cred-weight"
                  type="number"
                  min="1"
                  value={credForm.weight}
                  onChange={(e) => setCredForm({ ...credForm, weight: e.target.value })}
                />
              </div>
              <div className="form-actions">
                <button type="submit" className="btn-primary">
                  Add Credential
                </button>
                <button type="button" className="btn-secondary" onClick={() => setCredsModal(null)}>
                  Close
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default Providers;
