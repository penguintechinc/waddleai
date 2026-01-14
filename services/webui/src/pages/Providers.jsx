import { useState, useEffect } from 'react';
import axios from 'axios';
import './Providers.css';

function Providers() {
  const [providers, setProviders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [editingProvider, setEditingProvider] = useState(null);
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
      const response = await axios.get('/api/v1/providers');
      setProviders(response.data.providers || []);
      setError(null);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to fetch providers');
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
      setError(err.response?.data?.error || 'Failed to create provider');
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
      setError(err.response?.data?.error || 'Failed to update provider');
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
      setError(err.response?.data?.error || 'Failed to delete provider');
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
      setError(err.response?.data?.error || 'Connection test failed');
    }
  };

  const handleSyncToAILB = async () => {
    try {
      await axios.post('/api/v1/ailb/sync');
      setSuccess('Providers synced to AILB successfully');
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to sync to AILB');
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
          <button className="btn-primary" onClick={handleSyncToAILB}>
            Sync to AILB
          </button>
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
                  <option value="openai">OpenAI</option>
                  <option value="anthropic">Anthropic</option>
                  <option value="ollama">Ollama</option>
                  <option value="azure">Azure OpenAI</option>
                  <option value="google">Google AI</option>
                  <option value="cohere">Cohere</option>
                  <option value="custom">Custom</option>
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
    </div>
  );
}

export default Providers;
