import { useState, useEffect, useCallback } from 'react';
import axios from 'axios';
import { scopeLabel } from './hooksConstants';

const DEFAULT_ENTRY_FORM = { scope_type: 'org', scope_ref: '', pattern: '', reason: '' };

// Tier-1 canonical denylist (spec §18.1): an always-on, unconditional floor
// no admin `hook_rules` allow-rule can weaken. The builtin seed patterns
// (`shared.security.hooks_denylist.BUILTIN_DENYLIST_PATTERNS`) are not DB
// rows -- there is nothing to edit or delete -- so this page sources two
// endpoints: `/hooks/policy` for the live *effective* merged pattern list
// (what adapters actually enforce, builtin + admin-added), and
// `/hooks/denylist` for the admin-manageable additions only. Cross-
// referencing the two (rather than hardcoding the builtin list here too)
// means this view can never drift from what the server actually enforces.
function HookDenylistTab({ isAdmin, organizationId, onError, onSuccess }) {
  const [effectivePatterns, setEffectivePatterns] = useState([]);
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [formData, setFormData] = useState(DEFAULT_ENTRY_FORM);

  const fetchAll = useCallback(async () => {
    try {
      setLoading(true);
      const [policyRes, entriesRes] = await Promise.all([
        axios.get('/api/v1/hooks/policy'),
        axios.get('/api/v1/hooks/denylist'),
      ]);
      setEffectivePatterns(policyRes.data.denylist_patterns || []);
      setEntries(entriesRes.data.data || []);
    } catch (err) {
      onError(err.response?.data?.error || 'Failed to fetch denylist');
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  const adminAddedPatterns = new Set(entries.map((e) => e.pattern));
  const isOwnRow = (entry) =>
    isAdmin || (entry.scope_type === 'org' && String(entry.scope_ref) === String(organizationId));

  const resetForm = () => setFormData({ ...DEFAULT_ENTRY_FORM, scope_type: isAdmin ? 'global' : 'org' });

  const handleAdd = async (e) => {
    e.preventDefault();
    if (
      !window.confirm(
        `Add "${formData.pattern}" to the ${
          formData.scope_type === 'global' ? 'global' : 'organization'
        } Tier-1 denylist? Every matching tool call will be blocked immediately.`,
      )
    ) {
      return;
    }
    try {
      const payload = { pattern: formData.pattern, reason: formData.reason || null };
      payload.scope_type = formData.scope_type;
      payload.scope_ref = formData.scope_type === 'org' ? formData.scope_ref || null : null;
      await axios.post('/api/v1/hooks/denylist', payload);
      onSuccess('Denylist entry added');
      setShowForm(false);
      resetForm();
      fetchAll();
    } catch (err) {
      onError(err.response?.data?.error || 'Failed to add denylist entry');
    }
  };

  const handleDelete = async (entry) => {
    if (!window.confirm(`Remove "${entry.pattern}" from the denylist? This loosens protection.`)) {
      return;
    }
    try {
      await axios.delete(`/api/v1/hooks/denylist/${entry.id}`);
      onSuccess('Denylist entry removed');
      fetchAll();
    } catch (err) {
      onError(err.response?.data?.error || 'Failed to remove denylist entry');
    }
  };

  if (loading) {
    return <div className="loading">Loading denylist...</div>;
  }

  return (
    <div className="hooks-denylist-tab">
      <div className="tab-header">
        <p>
          Tier-1 patterns are enforced unconditionally, before any admin rule or remote policy
          check runs -- there is no allow-rule that can override a denylist match.
        </p>
        <button
          className="btn-primary"
          onClick={() => {
            resetForm();
            setShowForm(true);
          }}
        >
          + Add Pattern
        </button>
      </div>

      <div className="hooks-table">
        <table>
          <thead>
            <tr>
              <th>Pattern</th>
              <th>Source</th>
              <th>Scope / Reason</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {effectivePatterns.map((pattern) => {
              const adminEntry = entries.find((e) => e.pattern === pattern);
              const isBuiltin = !adminAddedPatterns.has(pattern);
              return (
                <tr key={pattern} data-testid="denylist-row">
                  <td>
                    <code>{pattern}</code>
                  </td>
                  <td>
                    <span className={`status-badge ${isBuiltin ? 'disabled' : 'active'}`}>
                      {isBuiltin ? 'Built-in' : 'Admin-added'}
                    </span>
                  </td>
                  <td>
                    {adminEntry
                      ? `${scopeLabel(adminEntry.scope_type, adminEntry.scope_ref)}${
                          adminEntry.reason ? ` -- ${adminEntry.reason}` : ''
                        }`
                      : 'Always enforced, every organization'}
                  </td>
                  <td>
                    {adminEntry && isOwnRow(adminEntry) ? (
                      <button
                        className="btn-small btn-danger"
                        onClick={() => handleDelete(adminEntry)}
                      >
                        Remove
                      </button>
                    ) : (
                      <span className="limit-text">{isBuiltin ? 'Not removable' : 'Read-only'}</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {showForm && (
        <div className="modal" data-testid="denylist-modal">
          <div className="modal-content">
            <h2>Add Denylist Pattern</h2>
            <form onSubmit={handleAdd}>
              {isAdmin ? (
                <div className="form-group">
                  <label htmlFor="denylist-scope-type">Scope</label>
                  <select
                    id="denylist-scope-type"
                    value={formData.scope_type}
                    onChange={(e) => setFormData({ ...formData, scope_type: e.target.value })}
                  >
                    <option value="global">Global (all organizations)</option>
                    <option value="org">Specific Organization</option>
                  </select>
                  {formData.scope_type === 'org' && (
                    <input
                      type="text"
                      aria-label="Organization ID"
                      placeholder="Organization ID"
                      value={formData.scope_ref}
                      onChange={(e) => setFormData({ ...formData, scope_ref: e.target.value })}
                      required
                    />
                  )}
                </div>
              ) : (
                <div className="form-group">
                  <label>Scope</label>
                  <p className="scope-notice">This pattern will apply to your organization only.</p>
                </div>
              )}
              <div className="form-group">
                <label htmlFor="denylist-pattern">Pattern</label>
                <input
                  id="denylist-pattern"
                  type="text"
                  value={formData.pattern}
                  onChange={(e) => setFormData({ ...formData, pattern: e.target.value })}
                  placeholder="e.g. *.tfstate, secrets/**"
                  required
                />
              </div>
              <div className="form-group">
                <label htmlFor="denylist-reason">Reason (optional)</label>
                <input
                  id="denylist-reason"
                  type="text"
                  value={formData.reason}
                  onChange={(e) => setFormData({ ...formData, reason: e.target.value })}
                />
              </div>
              <div className="form-actions">
                <button type="submit" className="btn-primary">
                  Add Pattern
                </button>
                <button type="button" className="btn-secondary" onClick={() => setShowForm(false)}>
                  Cancel
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default HookDenylistTab;
