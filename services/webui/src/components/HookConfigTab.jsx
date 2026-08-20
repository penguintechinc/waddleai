import { useState, useEffect, useCallback } from 'react';
import axios from 'axios';
import { DEFAULT_CONFIG_FORM, REMOTE_EVAL_FAIL_MODES, scopeLabel } from './hooksConstants';

// Per-scope Tier-2 opt-in + telemetry-capture opt-in (`hook_configs`, spec
// §18.2/§18.5). `capture_raw_payloads` is the privacy-sensitive field here:
// `tool_input` is command lines and absolute file paths, hashed by default
// and never persisted raw unless this is explicitly turned on -- the
// warning copy below is not optional decoration, it is the disclosure that
// makes flipping the switch an informed choice.
function HookConfigTab({ isAdmin, organizationId, onError, onSuccess }) {
  const [configs, setConfigs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [formScope, setFormScope] = useState(isAdmin ? 'global' : 'org');
  const [formOrgRef, setFormOrgRef] = useState(isAdmin ? '' : String(organizationId || ''));
  const [formData, setFormData] = useState(DEFAULT_CONFIG_FORM);
  const [saving, setSaving] = useState(false);

  const fetchConfigs = useCallback(async () => {
    try {
      setLoading(true);
      const response = await axios.get('/api/v1/hooks/configs');
      setConfigs(response.data.data || []);
    } catch (err) {
      onError(err.response?.data?.error || 'Failed to fetch hook configs');
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => {
    fetchConfigs();
  }, [fetchConfigs]);

  const loadRowIntoForm = (row) => {
    setFormScope(row.scope_type);
    setFormOrgRef(row.scope_ref || '');
    setFormData({
      remote_eval_enabled: row.remote_eval_enabled,
      remote_eval_timeout_ms: row.remote_eval_timeout_ms,
      remote_eval_fail_mode: row.remote_eval_fail_mode,
      capture_raw_payloads: row.capture_raw_payloads,
    });
  };

  const handleSave = async (e) => {
    e.preventDefault();
    if (
      formData.capture_raw_payloads &&
      !window.confirm(
        'Enabling raw payload capture will start storing full command lines and absolute file ' +
          'paths from every hook event for this scope. Continue?',
      )
    ) {
      return;
    }
    try {
      setSaving(true);
      const payload = { ...formData, remote_eval_timeout_ms: parseInt(formData.remote_eval_timeout_ms, 10) };
      if (isAdmin) {
        payload.scope_type = formScope;
        payload.scope_ref = formScope === 'org' ? formOrgRef || null : null;
      }
      await axios.post('/api/v1/hooks/configs', payload);
      onSuccess('Hook config saved');
      fetchConfigs();
    } catch (err) {
      onError(err.response?.data?.error || 'Failed to save hook config');
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return <div className="loading">Loading hook configuration...</div>;
  }

  return (
    <div className="hooks-config-tab">
      <p>
        Tier-2 remote policy evaluation is opt-in per scope; if it times out or errors, the
        configured fail mode decides whether the tool call is allowed or blocked.
      </p>

      {configs.length > 0 && (
        <div className="hooks-table">
          <table>
            <thead>
              <tr>
                <th>Scope</th>
                <th>Tier-2</th>
                <th>Timeout</th>
                <th>Fail Mode</th>
                <th>Raw Payload Capture</th>
                {isAdmin && <th>Actions</th>}
              </tr>
            </thead>
            <tbody>
              {configs.map((row) => (
                <tr key={row.id} data-testid="hook-config-row">
                  <td>{scopeLabel(row.scope_type, row.scope_ref)}</td>
                  <td>{row.remote_eval_enabled ? 'Enabled' : 'Disabled'}</td>
                  <td>{row.remote_eval_timeout_ms}ms</td>
                  <td>
                    <span className={`status-badge ${row.remote_eval_fail_mode === 'closed' ? 'error' : 'warning'}`}>
                      Fail {row.remote_eval_fail_mode}
                    </span>
                  </td>
                  <td>
                    <span className={`status-badge ${row.capture_raw_payloads ? 'error' : 'disabled'}`}>
                      {row.capture_raw_payloads ? 'ON' : 'Off'}
                    </span>
                  </td>
                  {isAdmin && (
                    <td>
                      <button className="btn-small btn-secondary" onClick={() => loadRowIntoForm(row)}>
                        Edit
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="hooks-config-card">
        <h3>{isAdmin ? 'Save Config' : "Your Organization's Hook Config"}</h3>
        <form onSubmit={handleSave}>
          {isAdmin && (
            <div className="form-row">
              <div className="form-group">
                <label htmlFor="config-scope-type">Scope</label>
                <select
                  id="config-scope-type"
                  value={formScope}
                  onChange={(e) => setFormScope(e.target.value)}
                >
                  <option value="global">Global default</option>
                  <option value="org">Specific Organization</option>
                </select>
              </div>
              {formScope === 'org' && (
                <div className="form-group">
                  <label htmlFor="config-org-ref">Organization ID</label>
                  <input
                    id="config-org-ref"
                    type="text"
                    value={formOrgRef}
                    onChange={(e) => setFormOrgRef(e.target.value)}
                    required
                  />
                </div>
              )}
            </div>
          )}

          <div className="form-group checkbox-group">
            <label htmlFor="config-remote-eval">
              <input
                id="config-remote-eval"
                type="checkbox"
                checked={formData.remote_eval_enabled}
                onChange={(e) => setFormData({ ...formData, remote_eval_enabled: e.target.checked })}
              />
              Enable Tier-2 remote policy evaluation
            </label>
          </div>

          <div className="form-row">
            <div className="form-group">
              <label htmlFor="config-timeout">Tier-2 Timeout (ms)</label>
              <input
                id="config-timeout"
                type="number"
                value={formData.remote_eval_timeout_ms}
                onChange={(e) =>
                  setFormData({ ...formData, remote_eval_timeout_ms: e.target.value })
                }
                disabled={!formData.remote_eval_enabled}
              />
            </div>
            <div className="form-group">
              <label htmlFor="config-fail-mode">On Tier-2 Timeout/Error</label>
              <select
                id="config-fail-mode"
                value={formData.remote_eval_fail_mode}
                onChange={(e) => setFormData({ ...formData, remote_eval_fail_mode: e.target.value })}
                disabled={!formData.remote_eval_enabled}
              >
                {REMOTE_EVAL_FAIL_MODES.map((mode) => (
                  <option key={mode} value={mode}>
                    Fail {mode === 'open' ? 'open (allow the tool call)' : 'closed (block it)'}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="form-group checkbox-group">
            <label htmlFor="config-capture-raw">
              <input
                id="config-capture-raw"
                type="checkbox"
                checked={formData.capture_raw_payloads}
                onChange={(e) => setFormData({ ...formData, capture_raw_payloads: e.target.checked })}
              />
              Capture raw tool payloads in telemetry
            </label>
            <p className="privacy-warning">
              Off by default: telemetry stores only a hash of each tool call. Turning this ON
              starts persisting the full, unredacted command line or file path from every hook
              event for this scope -- often sensitive, sometimes PII-adjacent. Only enable it if
              you specifically need raw payloads for investigation.
            </p>
          </div>

          <div className="form-actions">
            <button type="submit" className="btn-primary" disabled={saving}>
              {saving ? 'Saving...' : 'Save Config'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default HookConfigTab;
