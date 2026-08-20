import { useState, useEffect, useCallback } from 'react';
import axios from 'axios';
import { useAuth } from '../contexts/AuthContext';
import McpEndpointModal from '../components/McpEndpointModal';
import OpenCodeConfigCard from '../components/OpenCodeConfigCard';
import {
  TRANSPORT_LABELS,
  AUTH_TYPE_LABELS,
  IDENTITY_MODE_LABELS,
  DEFAULT_FORM,
  defaultAuthConfig,
  isSafeHttpUrl,
} from '../components/mcpEndpointConstants';
import './Integrations.css';

// WebUI surface for spec §11.4 (external MCP gateway) + the §11.3 OpenCode
// apparatus, backed by `services/management/app/api/v1/integrations.py`
// (already landed on this branch -- this page is a client of it, not a
// reimplementation). Two independent surfaces on one page:
//
//   1. MCP Endpoints (admin-only, org-scoped): register/edit/delete the
//      external `elder.*`-style MCP servers this org's assistants can
//      reach through WaddleAI's own `/mcp` surface. Every CRUD route is
//      `@require_role("admin")` server-side, so a non-admin gets 403 on
//      GET too -- there is currently no lower-privilege "browse registered
//      endpoints" API, so viewers see a notice instead of a fabricated
//      read-only list. Form rendering lives in McpEndpointModal.
//   2. OpenCode Config (self-service, any authenticated user) -- fully
//      self-contained in OpenCodeConfigCard; the virtual key never
//      touches localStorage, a URL, or a console.log call.
function Integrations() {
  const { user } = useAuth();
  const isAdmin = user?.role === 'admin';

  const [endpoints, setEndpoints] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  const [showCreateForm, setShowCreateForm] = useState(false);
  const [editingEndpoint, setEditingEndpoint] = useState(null);
  const [reconfigureAuth, setReconfigureAuth] = useState(false);
  const [formData, setFormData] = useState(DEFAULT_FORM);
  const [authConfig, setAuthConfig] = useState(defaultAuthConfig('none'));

  const [linkingEndpointId, setLinkingEndpointId] = useState(null);

  const fetchEndpoints = useCallback(async () => {
    try {
      setLoading(true);
      const response = await axios.get('/api/v1/integrations/mcp-endpoints');
      setEndpoints(response.data.data || []);
      setError(null);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to fetch MCP endpoints');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isAdmin) {
      fetchEndpoints();
    } else {
      setLoading(false);
    }
  }, [isAdmin, fetchEndpoints]);

  const resetForm = () => {
    setFormData(DEFAULT_FORM);
    setAuthConfig(defaultAuthConfig('none'));
    setReconfigureAuth(false);
  };

  const closeModal = () => {
    setShowCreateForm(false);
    setEditingEndpoint(null);
    resetForm();
  };

  const handleCreateEndpoint = async (e) => {
    e.preventDefault();
    try {
      const payload = { ...formData, auth_config: authConfig };
      await axios.post('/api/v1/integrations/mcp-endpoints', payload);
      setSuccess('MCP endpoint registered successfully');
      closeModal();
      fetchEndpoints();
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to register MCP endpoint');
    }
  };

  const handleUpdateEndpoint = async (e) => {
    e.preventDefault();
    try {
      // `auth_config` is stored as one JSON blob and PUT replaces it whole
      // (integrations.py: `update_fields["auth_config"] = ...`) -- there is
      // no per-field merge server-side. Omitting the key entirely when the
      // admin isn't reconfiguring auth is what keeps the existing encrypted
      // secret intact; sending a partial object here would silently wipe it.
      const payload = { ...formData };
      delete payload.namespace; // immutable after creation, not accepted by PUT
      if (reconfigureAuth) {
        payload.auth_config = authConfig;
      }
      await axios.put(`/api/v1/integrations/mcp-endpoints/${editingEndpoint.id}`, payload);
      setSuccess('MCP endpoint updated successfully');
      closeModal();
      fetchEndpoints();
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to update MCP endpoint');
    }
  };

  const handleDeleteEndpoint = async (endpointId) => {
    if (!window.confirm('Are you sure you want to delete this MCP endpoint?')) {
      return;
    }
    try {
      await axios.delete(`/api/v1/integrations/mcp-endpoints/${endpointId}`);
      setSuccess('MCP endpoint deleted successfully');
      fetchEndpoints();
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to delete MCP endpoint');
    }
  };

  const openEditForm = (endpoint) => {
    setEditingEndpoint(endpoint);
    setFormData({
      name: endpoint.name,
      url: endpoint.url,
      transport: endpoint.transport,
      auth_type: endpoint.auth_type,
      identity_mode: endpoint.identity_mode,
      namespace: endpoint.namespace,
      credentials_ref: endpoint.credentials_ref || '',
      status: endpoint.status || 'active',
    });
    setAuthConfig(defaultAuthConfig(endpoint.auth_type));
    setReconfigureAuth(false);
  };

  const handleLinkAccount = async (endpointId) => {
    try {
      setLinkingEndpointId(endpointId);
      const response = await axios.get(`/api/v1/integrations/mcp-endpoints/${endpointId}/link`);
      const authorizationUrl = response.data.data?.authorization_url;
      if (!isSafeHttpUrl(authorizationUrl)) {
        setError('Server returned an unexpected authorization URL; not opening it.');
        return;
      }
      window.open(authorizationUrl, '_blank', 'noopener,noreferrer');
      setSuccess('Complete sign-in in the new tab, then return here.');
      setTimeout(() => setSuccess(null), 5000);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to start the account link flow');
    } finally {
      setLinkingEndpointId(null);
    }
  };

  if (loading) {
    return <div className="loading">Loading integrations...</div>;
  }

  return (
    <div className="integrations">
      <div className="page-header">
        <h1>Integrations</h1>
        {isAdmin && (
          <button className="btn-primary" onClick={() => setShowCreateForm(true)}>
            + Register MCP Endpoint
          </button>
        )}
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

      <section className="integrations-card">
        <h3>External MCP Endpoints</h3>
        <p>
          Register external MCP servers whose tools are re-served through WaddleAI&apos;s own
          MCP surface, namespaced (<code>namespace.tool_name</code>) so they never collide with
          native tools.
        </p>

        {!isAdmin && (
          <div className="admin-required-notice">
            Managing MCP endpoints requires Admin access. If a <code>per_user</code> endpoint has
            already been registered by an admin, ask them for its endpoint ID and use{' '}
            <code>waddleai link &lt;endpoint-id&gt;</code> from the CLI to connect your own
            account.
          </div>
        )}

        {isAdmin && endpoints.length === 0 && (
          <div className="empty-state">
            <p>No MCP endpoints registered yet</p>
            <button className="btn-primary" onClick={() => setShowCreateForm(true)}>
              Register First Endpoint
            </button>
          </div>
        )}

        {isAdmin && endpoints.length > 0 && (
          <div className="endpoints-grid">
            {endpoints.map((endpoint) => (
              <div key={endpoint.id} className="endpoint-card" data-testid="endpoint-card">
                <div className="endpoint-header">
                  <h4>{endpoint.name}</h4>
                  <span className={`status-badge ${endpoint.status}`}>{endpoint.status}</span>
                </div>
                <div className="endpoint-details">
                  <div className="detail-row">
                    <span className="label">Namespace:</span>
                    <span className="value">{endpoint.namespace}</span>
                  </div>
                  <div className="detail-row">
                    <span className="label">URL:</span>
                    <span className="value">{endpoint.url}</span>
                  </div>
                  <div className="detail-row">
                    <span className="label">Transport:</span>
                    <span className="value">
                      {TRANSPORT_LABELS[endpoint.transport] || endpoint.transport}
                    </span>
                  </div>
                  <div className="detail-row">
                    <span className="label">Auth:</span>
                    <span className="value">
                      {AUTH_TYPE_LABELS[endpoint.auth_type] || endpoint.auth_type}
                    </span>
                  </div>
                  <div className="detail-row">
                    <span className="label">Identity:</span>
                    <span className="value">
                      {IDENTITY_MODE_LABELS[endpoint.identity_mode] || endpoint.identity_mode}
                    </span>
                  </div>
                </div>
                <div className="endpoint-actions">
                  {endpoint.identity_mode === 'per_user' && (
                    <button
                      className="btn-small btn-primary"
                      onClick={() => handleLinkAccount(endpoint.id)}
                      disabled={linkingEndpointId === endpoint.id}
                    >
                      {linkingEndpointId === endpoint.id ? 'Opening...' : 'Link My Account'}
                    </button>
                  )}
                  <button className="btn-small btn-secondary" onClick={() => openEditForm(endpoint)}>
                    Edit
                  </button>
                  <button
                    className="btn-small btn-danger"
                    onClick={() => handleDeleteEndpoint(endpoint.id)}
                  >
                    Delete
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {(showCreateForm || editingEndpoint) && (
        <McpEndpointModal
          editingEndpoint={editingEndpoint}
          formData={formData}
          setFormData={setFormData}
          authConfig={authConfig}
          setAuthConfig={setAuthConfig}
          reconfigureAuth={reconfigureAuth}
          setReconfigureAuth={setReconfigureAuth}
          onSubmit={editingEndpoint ? handleUpdateEndpoint : handleCreateEndpoint}
          onCancel={closeModal}
        />
      )}

      <OpenCodeConfigCard />
    </div>
  );
}

export default Integrations;
