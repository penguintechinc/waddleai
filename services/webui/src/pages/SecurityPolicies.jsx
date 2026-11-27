import { useState, useEffect } from 'react';
import {
  listSecurityPolicies,
  resolveSecurityPolicy,
  upsertSecurityPolicy,
  updateSecurityPolicy,
  deleteSecurityPolicy,
  listBypassGrants,
  createBypassGrant,
  revokeBypassGrant,
  getCiliumStatus,
  reconcileCilium,
  rotateSigningKey,
} from '../api/adminApi';
import './SecurityPolicies.css';

const EMPTY_POLICY = {
  scope_type: 'global',
  scope_ref: '',
  direction: 'both',
  tier1_enabled: true,
  tier2_enabled: true,
  tier3_enabled: false,
  tier4_enabled: false,
  block_action: 'block',
  fail_mode: 'closed',
};

function SecurityPolicies() {
  const [policies, setPolicies] = useState([]);
  const [grants, setGrants] = useState([]);
  const [cilium, setCilium] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  const [policyForm, setPolicyForm] = useState(EMPTY_POLICY);
  const [editingPolicy, setEditingPolicy] = useState(null);

  const [resolveQuery, setResolveQuery] = useState({
    org: '',
    model: '',
    tool: '',
    direction: 'both',
  });
  const [resolvedPolicy, setResolvedPolicy] = useState(null);

  const [grantForm, setGrantForm] = useState({
    subject_type: 'user',
    subject_ref: '',
    mode: 'shadow',
    expires_at: '',
    include_upstream: false,
  });

  const [signingResult, setSigningResult] = useState(null);

  useEffect(() => {
    fetchAll();
  }, []);

  const flashSuccess = (msg) => {
    setSuccess(msg);
    setTimeout(() => setSuccess(null), 3000);
  };

  const fetchAll = async () => {
    try {
      setLoading(true);
      const [polRes, grantRes, ciliumRes] = await Promise.all([
        listSecurityPolicies().catch(() => []),
        listBypassGrants().catch(() => []),
        getCiliumStatus().catch(() => null),
      ]);
      setPolicies(Array.isArray(polRes) ? polRes : []);
      setGrants(Array.isArray(grantRes) ? grantRes : []);
      setCilium(ciliumRes);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleUpsertPolicy = async (e) => {
    e.preventDefault();
    try {
      const payload = {
        scope_type: policyForm.scope_type,
        scope_ref: policyForm.scope_type === 'global' ? null : policyForm.scope_ref,
        direction: policyForm.direction,
        tier1_enabled: policyForm.tier1_enabled,
        tier2_enabled: policyForm.tier2_enabled,
        tier3_enabled: policyForm.tier3_enabled,
        tier4_enabled: policyForm.tier4_enabled,
        block_action: policyForm.block_action,
        fail_mode: policyForm.fail_mode,
      };
      await upsertSecurityPolicy(payload);
      setPolicyForm(EMPTY_POLICY);
      setError(null);
      flashSuccess('Security policy saved');
      fetchAll();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleUpdatePolicy = async (e) => {
    e.preventDefault();
    try {
      await updateSecurityPolicy(editingPolicy.id, {
        tier1_enabled: editingPolicy.tier1_enabled,
        tier2_enabled: editingPolicy.tier2_enabled,
        tier3_enabled: editingPolicy.tier3_enabled,
        tier4_enabled: editingPolicy.tier4_enabled,
        block_action: editingPolicy.block_action,
        fail_mode: editingPolicy.fail_mode,
      });
      setEditingPolicy(null);
      setError(null);
      flashSuccess('Security policy updated');
      fetchAll();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleDeletePolicy = async (id) => {
    if (!window.confirm('Delete this security policy?')) return;
    try {
      await deleteSecurityPolicy(id);
      setError(null);
      flashSuccess('Security policy deleted');
      fetchAll();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleResolvePolicy = async (e) => {
    e.preventDefault();
    try {
      const params = { direction: resolveQuery.direction };
      if (resolveQuery.org) params.org = resolveQuery.org;
      if (resolveQuery.model) params.model = resolveQuery.model;
      if (resolveQuery.tool) params.tool = resolveQuery.tool;
      const res = await resolveSecurityPolicy(params);
      setResolvedPolicy(res);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleCreateGrant = async (e) => {
    e.preventDefault();
    try {
      await createBypassGrant({
        subject_type: grantForm.subject_type,
        subject_ref: grantForm.subject_ref,
        mode: grantForm.mode,
        expires_at: grantForm.expires_at,
        include_upstream: grantForm.include_upstream,
      });
      setGrantForm({
        subject_type: 'user',
        subject_ref: '',
        mode: 'shadow',
        expires_at: '',
        include_upstream: false,
      });
      setError(null);
      flashSuccess('Bypass grant created');
      fetchAll();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleRevokeGrant = async (id) => {
    try {
      await revokeBypassGrant(id);
      setError(null);
      flashSuccess('Bypass grant revoked');
      fetchAll();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleReconcileCilium = async () => {
    try {
      const res = await reconcileCilium();
      setError(null);
      flashSuccess(res?.reason || 'Cilium reconcile triggered');
      fetchAll();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleRotateSigningKey = async () => {
    if (!window.confirm('Rotate the active OIDC signing key?')) return;
    try {
      const res = await rotateSigningKey();
      setSigningResult(res);
      setError(null);
      flashSuccess(`Rotated signing key to ${res?.active_kid}`);
    } catch (err) {
      setError(err.message);
    }
  };

  if (loading) {
    return <div className="admin-loading">Loading security policies...</div>;
  }

  return (
    <div className="security-policies-page">
      <div className="page-header">
        <h1>Security Policies &amp; Cluster Controls</h1>
        <div className="header-actions">
          <button type="button" className="btn-secondary" onClick={handleReconcileCilium}>
            Reconcile Cilium
          </button>
          <button type="button" className="btn-primary" onClick={handleRotateSigningKey}>
            Rotate Signing Key
          </button>
        </div>
      </div>

      {error && <div className="alert alert-error">{error}</div>}
      {success && <div className="alert alert-success">{success}</div>}

      <div className="admin-form-card">
        <h2>Cluster Network &amp; Identity Status</h2>
        <div className="stats-row">
          <div className="stat-box">
            <span className="stat-label">Cilium Native Rate Limit</span>
            <span className="stat-value">{cilium?.flag_enabled ? 'Enabled' : 'Disabled'}</span>
          </div>
          <div className="stat-box">
            <span className="stat-label">Cilium Degraded</span>
            <span className="stat-value">{cilium?.degraded ? 'Yes' : 'No'}</span>
          </div>
          {signingResult && (
            <div className="stat-box">
              <span className="stat-label">Active Signing KID</span>
              <span className="stat-value">{signingResult.active_kid}</span>
            </div>
          )}
        </div>
      </div>

      <form className="admin-form-card" onSubmit={handleUpsertPolicy}>
        <h2>Upsert Security Policy</h2>
        <div className="form-grid">
          <label>
            Scope Type
            <select
              value={policyForm.scope_type}
              onChange={(e) => setPolicyForm({ ...policyForm, scope_type: e.target.value })}
            >
              <option value="global">global</option>
              <option value="org">org</option>
              <option value="model">model</option>
              <option value="tool">tool</option>
            </select>
          </label>
          {policyForm.scope_type !== 'global' && (
            <label>
              Scope Ref
              <input
                type="text"
                required
                value={policyForm.scope_ref}
                onChange={(e) => setPolicyForm({ ...policyForm, scope_ref: e.target.value })}
              />
            </label>
          )}
          <label>
            Direction
            <select
              value={policyForm.direction}
              onChange={(e) => setPolicyForm({ ...policyForm, direction: e.target.value })}
            >
              <option value="both">both</option>
              <option value="input">input</option>
              <option value="output">output</option>
            </select>
          </label>
          <label>
            Block Action
            <input
              type="text"
              value={policyForm.block_action}
              onChange={(e) => setPolicyForm({ ...policyForm, block_action: e.target.value })}
            />
          </label>
          <label>
            Fail Mode
            <input
              type="text"
              value={policyForm.fail_mode}
              onChange={(e) => setPolicyForm({ ...policyForm, fail_mode: e.target.value })}
            />
          </label>
        </div>
        <div className="tier-toggles">
          <label>
            <input
              type="checkbox"
              checked={policyForm.tier1_enabled}
              onChange={(e) => setPolicyForm({ ...policyForm, tier1_enabled: e.target.checked })}
            />
            Tier 1
          </label>
          <label>
            <input
              type="checkbox"
              checked={policyForm.tier2_enabled}
              onChange={(e) => setPolicyForm({ ...policyForm, tier2_enabled: e.target.checked })}
            />
            Tier 2
          </label>
          <label>
            <input
              type="checkbox"
              checked={policyForm.tier3_enabled}
              onChange={(e) => setPolicyForm({ ...policyForm, tier3_enabled: e.target.checked })}
            />
            Tier 3
          </label>
          <label>
            <input
              type="checkbox"
              checked={policyForm.tier4_enabled}
              onChange={(e) => setPolicyForm({ ...policyForm, tier4_enabled: e.target.checked })}
            />
            Tier 4
          </label>
        </div>
        <div className="form-actions">
          <button type="submit" className="btn-primary">Save Policy</button>
        </div>
      </form>

      {editingPolicy && (
        <form className="admin-form-card" onSubmit={handleUpdatePolicy}>
          <h2>Edit Policy #{editingPolicy.id}</h2>
          <div className="form-grid">
            <label>
              Block Action
              <input
                type="text"
                value={editingPolicy.block_action || ''}
                onChange={(e) => setEditingPolicy({ ...editingPolicy, block_action: e.target.value })}
              />
            </label>
            <label>
              Fail Mode
              <input
                type="text"
                value={editingPolicy.fail_mode || ''}
                onChange={(e) => setEditingPolicy({ ...editingPolicy, fail_mode: e.target.value })}
              />
            </label>
          </div>
          <div className="form-actions">
            <button type="submit" className="btn-primary">Update</button>
            <button type="button" className="btn-secondary" onClick={() => setEditingPolicy(null)}>
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
              <th>Direction</th>
              <th>Tiers</th>
              <th>Action / Fail Mode</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {policies.length === 0 ? (
              <tr>
                <td colSpan="6" className="empty-cell">No security policies configured</td>
              </tr>
            ) : (
              policies.map((p) => (
                <tr key={p.id}>
                  <td>{p.id}</td>
                  <td>{p.scope_type}{p.scope_ref ? `:${p.scope_ref}` : ''}</td>
                  <td>{p.direction}</td>
                  <td>
                    {[
                      p.tier1_enabled && 'T1',
                      p.tier2_enabled && 'T2',
                      p.tier3_enabled && 'T3',
                      p.tier4_enabled && 'T4',
                    ]
                      .filter(Boolean)
                      .join(', ') || 'None'}
                  </td>
                  <td>{p.block_action || '-'} / {p.fail_mode || '-'}</td>
                  <td className="actions-cell">
                    <button type="button" className="btn-sm" onClick={() => setEditingPolicy({ ...p })}>
                      Edit
                    </button>
                    <button type="button" className="btn-sm btn-danger" onClick={() => handleDeletePolicy(p.id)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <div className="admin-form-card">
        <h2>Policy Resolution Preview</h2>
        <form className="inline-form" onSubmit={handleResolvePolicy}>
          <input
            type="text"
            placeholder="Org ID"
            value={resolveQuery.org}
            onChange={(e) => setResolveQuery({ ...resolveQuery, org: e.target.value })}
          />
          <input
            type="text"
            placeholder="Model"
            value={resolveQuery.model}
            onChange={(e) => setResolveQuery({ ...resolveQuery, model: e.target.value })}
          />
          <input
            type="text"
            placeholder="Tool"
            value={resolveQuery.tool}
            onChange={(e) => setResolveQuery({ ...resolveQuery, tool: e.target.value })}
          />
          <select
            value={resolveQuery.direction}
            onChange={(e) => setResolveQuery({ ...resolveQuery, direction: e.target.value })}
          >
            <option value="both">both</option>
            <option value="input">input</option>
            <option value="output">output</option>
          </select>
          <button type="submit" className="btn-secondary">Resolve</button>
        </form>
        {resolvedPolicy && (
          <pre className="json-preview">{JSON.stringify(resolvedPolicy, null, 2)}</pre>
        )}
      </div>

      <div className="admin-form-card">
        <h2>Bypass Grants</h2>
        <form className="inline-form" onSubmit={handleCreateGrant}>
          <select
            value={grantForm.subject_type}
            onChange={(e) => setGrantForm({ ...grantForm, subject_type: e.target.value })}
          >
            <option value="user">user</option>
            <option value="vkey">vkey</option>
          </select>
          <input
            type="text"
            placeholder="Subject Ref (ID)"
            required
            value={grantForm.subject_ref}
            onChange={(e) => setGrantForm({ ...grantForm, subject_ref: e.target.value })}
          />
          <select
            value={grantForm.mode}
            onChange={(e) => setGrantForm({ ...grantForm, mode: e.target.value })}
          >
            <option value="shadow">shadow</option>
            <option value="skip">skip</option>
          </select>
          <input
            type="text"
            placeholder="Expires At (ISO-8601)"
            required
            value={grantForm.expires_at}
            onChange={(e) => setGrantForm({ ...grantForm, expires_at: e.target.value })}
          />
          <button type="submit" className="btn-primary">Create Grant</button>
        </form>
        <table className="sub-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>Subject</th>
              <th>Mode</th>
              <th>Expires At</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {grants.length === 0 ? (
              <tr>
                <td colSpan="5" className="empty-cell">No active bypass grants</td>
              </tr>
            ) : (
              grants.map((g) => (
                <tr key={g.id}>
                  <td>{g.id}</td>
                  <td>{g.subject_type}:{g.subject_ref}</td>
                  <td>{g.mode}</td>
                  <td>{g.expires_at}</td>
                  <td>
                    <button type="button" className="btn-sm btn-danger" onClick={() => handleRevokeGrant(g.id)}>
                      Revoke
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default SecurityPolicies;
