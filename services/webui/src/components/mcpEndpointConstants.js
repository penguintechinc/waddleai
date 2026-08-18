// Shared constants for the MCP Endpoints surface (spec §11.4), split out of
// Integrations.jsx so both the page (endpoint card display) and the
// register/edit modal (form selects) source the same option lists and
// defaults without duplicating them.

export const VALID_TRANSPORTS = ['streamable_http', 'stdio'];
export const TRANSPORT_LABELS = {
  streamable_http: 'Streamable HTTP',
  stdio: 'stdio (subprocess)',
};

export const VALID_AUTH_TYPES = ['none', 'header', 'oauth2_client_credentials', 'oauth2_auth_code'];
export const AUTH_TYPE_LABELS = {
  none: 'None',
  header: 'Static Header',
  oauth2_client_credentials: 'OAuth2 Client Credentials',
  oauth2_auth_code: 'OAuth2 Authorization Code (+ DCR)',
};

export const VALID_IDENTITY_MODES = ['shared', 'per_user'];
export const IDENTITY_MODE_LABELS = {
  shared: 'Shared (one org-wide credential)',
  per_user: 'Per-User (each caller links their own account)',
};

export const DEFAULT_FORM = {
  name: '',
  url: '',
  transport: 'streamable_http',
  auth_type: 'none',
  identity_mode: 'shared',
  namespace: '',
  credentials_ref: '',
  status: 'active',
};

// Only `header_value` and `client_secret` are encrypted/masked server-side
// (`_SECRET_AUTH_CONFIG_FIELDS` in integrations.py) -- everything else in
// `auth_config` is plain configuration, not a credential.
export function defaultAuthConfig(authType) {
  switch (authType) {
    case 'header':
      return { header_name: 'Authorization', header_value: '' };
    case 'oauth2_client_credentials':
      return { token_endpoint: '', client_id: '', client_secret: '', scope: '' };
    case 'oauth2_auth_code':
      return {
        authorization_endpoint: '',
        token_endpoint: '',
        registration_endpoint: '',
        client_id: '',
        client_secret: '',
        scope: '',
      };
    case 'none':
    default:
      return {};
  }
}

// Only allow http(s) URLs to be opened/followed. `authorization_url` is
// server-built from admin-configured `auth_config.authorization_endpoint`
// -- an externally-pointed value the caller does not control at render
// time -- so it's treated the same as any other untrusted external string
// reaching the UI: validated before being used to drive browser navigation,
// never interpolated into markup.
export function isSafeHttpUrl(value) {
  return typeof value === 'string' && /^https?:\/\//i.test(value);
}
