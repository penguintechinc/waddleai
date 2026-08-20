import {
  VALID_TRANSPORTS,
  TRANSPORT_LABELS,
  VALID_AUTH_TYPES,
  AUTH_TYPE_LABELS,
  VALID_IDENTITY_MODES,
  IDENTITY_MODE_LABELS,
  defaultAuthConfig,
} from './mcpEndpointConstants';

// Register/edit form for one `mcp_endpoints` row. Split out of Integrations.jsx
// to keep that file under the project's 25,000-character limit -- all
// persistence (POST/PUT) and the "keep existing encrypted auth_config unless
// explicitly reconfiguring" logic stay in the parent, which owns formData/
// authConfig state and passes them down.
function renderAuthConfigFields(authType, authConfig, setAuthConfig) {
  if (authType === 'header') {
    return [
      <div className="form-group" key="header_name">
        <label htmlFor="auth-header-name">Header Name</label>
        <input
          id="auth-header-name"
          type="text"
          value={authConfig.header_name || ''}
          onChange={(e) => setAuthConfig({ ...authConfig, header_name: e.target.value })}
          placeholder="Authorization"
          required
        />
      </div>,
      <div className="form-group" key="header_value">
        <label htmlFor="auth-header-value">Header Value</label>
        <input
          id="auth-header-value"
          type="password"
          value={authConfig.header_value || ''}
          onChange={(e) => setAuthConfig({ ...authConfig, header_value: e.target.value })}
          placeholder="Bearer sk-..."
          required
        />
        <small>Encrypted at rest; never echoed back by the server.</small>
      </div>,
    ];
  }
  if (authType === 'oauth2_client_credentials') {
    return [
      <div className="form-group" key="token_endpoint">
        <label htmlFor="auth-token-endpoint">Token Endpoint</label>
        <input
          id="auth-token-endpoint"
          type="url"
          value={authConfig.token_endpoint || ''}
          onChange={(e) => setAuthConfig({ ...authConfig, token_endpoint: e.target.value })}
          placeholder="https://idp.example.com/oauth2/token"
          required
        />
      </div>,
      <div className="form-group" key="client_id">
        <label htmlFor="auth-client-id">Client ID</label>
        <input
          id="auth-client-id"
          type="text"
          value={authConfig.client_id || ''}
          onChange={(e) => setAuthConfig({ ...authConfig, client_id: e.target.value })}
          required
        />
      </div>,
      <div className="form-group" key="client_secret">
        <label htmlFor="auth-client-secret">Client Secret</label>
        <input
          id="auth-client-secret"
          type="password"
          value={authConfig.client_secret || ''}
          onChange={(e) => setAuthConfig({ ...authConfig, client_secret: e.target.value })}
          required
        />
        <small>Encrypted at rest; never echoed back by the server.</small>
      </div>,
      <div className="form-group" key="scope">
        <label htmlFor="auth-scope">Scope (optional)</label>
        <input
          id="auth-scope"
          type="text"
          value={authConfig.scope || ''}
          onChange={(e) => setAuthConfig({ ...authConfig, scope: e.target.value })}
        />
      </div>,
    ];
  }
  if (authType === 'oauth2_auth_code') {
    return [
      <div className="form-group" key="authorization_endpoint">
        <label htmlFor="auth-authorization-endpoint">Authorization Endpoint</label>
        <input
          id="auth-authorization-endpoint"
          type="url"
          value={authConfig.authorization_endpoint || ''}
          onChange={(e) => setAuthConfig({ ...authConfig, authorization_endpoint: e.target.value })}
          placeholder="https://idp.example.com/oauth2/authorize"
          required
        />
      </div>,
      <div className="form-group" key="token_endpoint">
        <label htmlFor="auth-token-endpoint">Token Endpoint</label>
        <input
          id="auth-token-endpoint"
          type="url"
          value={authConfig.token_endpoint || ''}
          onChange={(e) => setAuthConfig({ ...authConfig, token_endpoint: e.target.value })}
          placeholder="https://idp.example.com/oauth2/token"
          required
        />
      </div>,
      <div className="form-group" key="registration_endpoint">
        <label htmlFor="auth-registration-endpoint">
          Dynamic Client Registration Endpoint (optional)
        </label>
        <input
          id="auth-registration-endpoint"
          type="url"
          value={authConfig.registration_endpoint || ''}
          onChange={(e) => setAuthConfig({ ...authConfig, registration_endpoint: e.target.value })}
          placeholder="https://idp.example.com/oauth2/register"
        />
        <small>Leave blank if you already have a static client_id below (RFC 7591).</small>
      </div>,
      <div className="form-group" key="client_id">
        <label htmlFor="auth-client-id">Client ID (optional if using DCR)</label>
        <input
          id="auth-client-id"
          type="text"
          value={authConfig.client_id || ''}
          onChange={(e) => setAuthConfig({ ...authConfig, client_id: e.target.value })}
        />
      </div>,
      <div className="form-group" key="client_secret">
        <label htmlFor="auth-client-secret">Client Secret (optional if using DCR)</label>
        <input
          id="auth-client-secret"
          type="password"
          value={authConfig.client_secret || ''}
          onChange={(e) => setAuthConfig({ ...authConfig, client_secret: e.target.value })}
        />
        <small>Encrypted at rest; never echoed back by the server.</small>
      </div>,
      <div className="form-group" key="scope">
        <label htmlFor="auth-scope">Scope (optional)</label>
        <input
          id="auth-scope"
          type="text"
          value={authConfig.scope || ''}
          onChange={(e) => setAuthConfig({ ...authConfig, scope: e.target.value })}
        />
      </div>,
    ];
  }
  return [];
}

function McpEndpointModal({
  editingEndpoint,
  formData,
  setFormData,
  authConfig,
  setAuthConfig,
  reconfigureAuth,
  setReconfigureAuth,
  onSubmit,
  onCancel,
}) {
  const handleAuthTypeChange = (authType) => {
    setFormData({ ...formData, auth_type: authType });
    setAuthConfig(defaultAuthConfig(authType));
  };

  return (
    <div className="modal">
      <div className="modal-content">
        <h2>{editingEndpoint ? 'Edit MCP Endpoint' : 'Register MCP Endpoint'}</h2>
        <form onSubmit={onSubmit}>
          <div className="form-group">
            <label htmlFor="endpoint-name">Name</label>
            <input
              id="endpoint-name"
              type="text"
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              placeholder="Elder Docs MCP"
              required
            />
          </div>

          <div className="form-group">
            <label htmlFor="endpoint-namespace">Namespace</label>
            <input
              id="endpoint-namespace"
              type="text"
              value={formData.namespace}
              onChange={(e) => setFormData({ ...formData, namespace: e.target.value })}
              placeholder="elder"
              pattern="[A-Za-z0-9_-]+"
              required
              readOnly={!!editingEndpoint}
              disabled={!!editingEndpoint}
            />
            <small>
              {editingEndpoint
                ? 'Namespace cannot be changed after registration.'
                : 'Alphanumeric, - and _ only. Tools appear as namespace.tool_name.'}
            </small>
          </div>

          <div className="form-group">
            <label htmlFor="endpoint-url">URL</label>
            <input
              id="endpoint-url"
              type="url"
              value={formData.url}
              onChange={(e) => setFormData({ ...formData, url: e.target.value })}
              placeholder="https://mcp.example.com/mcp"
              required
            />
          </div>

          <div className="form-group">
            <label htmlFor="endpoint-transport">Transport</label>
            <select
              id="endpoint-transport"
              value={formData.transport}
              onChange={(e) => setFormData({ ...formData, transport: e.target.value })}
            >
              {VALID_TRANSPORTS.map((t) => (
                <option key={t} value={t}>
                  {TRANSPORT_LABELS[t]}
                </option>
              ))}
            </select>
          </div>

          <div className="form-group">
            <label htmlFor="endpoint-identity-mode">Identity Mode</label>
            <select
              id="endpoint-identity-mode"
              value={formData.identity_mode}
              onChange={(e) => setFormData({ ...formData, identity_mode: e.target.value })}
            >
              {VALID_IDENTITY_MODES.map((m) => (
                <option key={m} value={m}>
                  {IDENTITY_MODE_LABELS[m]}
                </option>
              ))}
            </select>
          </div>

          {editingEndpoint && (
            <div className="form-group">
              <label htmlFor="endpoint-status">Status</label>
              <select
                id="endpoint-status"
                value={formData.status}
                onChange={(e) => setFormData({ ...formData, status: e.target.value })}
              >
                <option value="active">Active</option>
                <option value="disabled">Disabled</option>
                <option value="error">Error</option>
              </select>
            </div>
          )}

          <div className="form-group">
            <label htmlFor="endpoint-credentials-ref">Credentials Reference (optional)</label>
            <input
              id="endpoint-credentials-ref"
              type="text"
              value={formData.credentials_ref}
              onChange={(e) => setFormData({ ...formData, credentials_ref: e.target.value })}
            />
          </div>

          <div className="form-group">
            <label htmlFor="endpoint-auth-type">Authentication</label>
            <select
              id="endpoint-auth-type"
              value={formData.auth_type}
              onChange={(e) => handleAuthTypeChange(e.target.value)}
              disabled={!!editingEndpoint && !reconfigureAuth}
            >
              {VALID_AUTH_TYPES.map((t) => (
                <option key={t} value={t}>
                  {AUTH_TYPE_LABELS[t]}
                </option>
              ))}
            </select>
          </div>

          {editingEndpoint && (
            <div className="form-group reconfigure-toggle">
              <label>
                <input
                  type="checkbox"
                  checked={reconfigureAuth}
                  onChange={(e) => {
                    setReconfigureAuth(e.target.checked);
                    setAuthConfig(defaultAuthConfig(formData.auth_type));
                  }}
                />
                Change authentication configuration
              </label>
              <small>
                Secret values (client secret / header value) are never returned by the server and
                must be re-entered in full if you change this endpoint&apos;s auth settings.
              </small>
            </div>
          )}

          {(!editingEndpoint || reconfigureAuth) &&
            renderAuthConfigFields(formData.auth_type, authConfig, setAuthConfig)}

          <div className="form-actions">
            <button type="submit" className="btn-primary">
              {editingEndpoint ? 'Update Endpoint' : 'Register Endpoint'}
            </button>
            <button type="button" className="btn-secondary" onClick={onCancel}>
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default McpEndpointModal;
