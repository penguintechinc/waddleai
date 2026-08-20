import { useState, useEffect, useCallback } from 'react';
import axios from 'axios';
import HookRuleModal from './HookRuleModal';
import {
  DEFAULT_RULE_FORM,
  DECISION_LABELS,
  DECISION_BADGE_CLASS,
  ECOSYSTEM_LABELS,
  EVENT_LABELS,
  scopeLabel,
  isDangerousRule,
} from './hooksConstants';

// Admin CRUD over `hook_rules` (spec §18.3/§18.4), backed by
// `services/management/app/api/v1/hook_rules.py`. `resource_manager` is
// force-scoped server-side to their own org on every write regardless of
// what the request body asks -- the UI mirrors that by simply not offering
// a scope selector (see HookRuleModal), but the 403 a stray cross-org write
// would get back is the real boundary, not this client-side omission.
function HookRulesTab({ isAdmin, organizationId, onError, onSuccess }) {
  const [rules, setRules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [editingRule, setEditingRule] = useState(null);
  const [formData, setFormData] = useState(DEFAULT_RULE_FORM);

  const fetchRules = useCallback(async () => {
    try {
      setLoading(true);
      const response = await axios.get('/api/v1/hooks/rules');
      setRules(response.data.data || []);
    } catch (err) {
      onError(err.response?.data?.error || 'Failed to fetch hook rules');
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => {
    fetchRules();
  }, [fetchRules]);

  const isOwnRow = (rule) =>
    isAdmin || (rule.scope_type === 'org' && String(rule.scope_ref) === String(organizationId));

  const resetForm = () => {
    setFormData({ ...DEFAULT_RULE_FORM, scope_type: isAdmin ? 'global' : 'org' });
    setEditingRule(null);
  };

  const closeModal = () => {
    setShowForm(false);
    resetForm();
  };

  // A live DENY rule can halt every developer in scope -- creating or
  // enabling one always requires this explicit confirmation, never a
  // silent save.
  const confirmIfDangerous = (decision, enabled, scopeDescription) => {
    if (!isDangerousRule(decision, enabled)) return true;
    return window.confirm(
      `This is a DENY rule that will block matching tool calls for ${scopeDescription}. Continue?`,
    );
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    const scopeDescription = editingRule
      ? scopeLabel(editingRule.scope_type, editingRule.scope_ref)
      : scopeLabel(formData.scope_type, formData.scope_ref || organizationId);
    if (!confirmIfDangerous(formData.decision, formData.enabled, scopeDescription)) {
      return;
    }
    try {
      const payload = {
        ecosystem: formData.ecosystem || null,
        event: formData.event || null,
        tool_name_pattern: formData.tool_name_pattern || null,
        match_pattern: formData.match_pattern || null,
        decision: formData.decision,
        reason: formData.reason,
        enabled: formData.enabled,
        priority: parseInt(formData.priority, 10) || 100,
      };
      if (editingRule) {
        await axios.put(`/api/v1/hooks/rules/${editingRule.id}`, payload);
        onSuccess('Hook rule updated');
      } else {
        payload.scope_type = formData.scope_type;
        payload.scope_ref = formData.scope_type === 'org' ? formData.scope_ref || null : null;
        await axios.post('/api/v1/hooks/rules', payload);
        onSuccess('Hook rule created');
      }
      closeModal();
      fetchRules();
    } catch (err) {
      onError(err.response?.data?.error || 'Failed to save hook rule');
    }
  };

  const handleToggleEnabled = async (rule) => {
    const nextEnabled = !rule.enabled;
    const scopeDescription = scopeLabel(rule.scope_type, rule.scope_ref);
    if (!confirmIfDangerous(rule.decision, nextEnabled, scopeDescription)) {
      return;
    }
    try {
      await axios.put(`/api/v1/hooks/rules/${rule.id}`, { enabled: nextEnabled });
      onSuccess(nextEnabled ? 'Rule enabled' : 'Rule disabled');
      fetchRules();
    } catch (err) {
      onError(err.response?.data?.error || 'Failed to update hook rule');
    }
  };

  const handleDelete = async (rule) => {
    if (!window.confirm(`Delete this hook rule (${scopeLabel(rule.scope_type, rule.scope_ref)})?`)) {
      return;
    }
    try {
      await axios.delete(`/api/v1/hooks/rules/${rule.id}`);
      onSuccess('Hook rule deleted');
      fetchRules();
    } catch (err) {
      onError(err.response?.data?.error || 'Failed to delete hook rule');
    }
  };

  const openEditForm = (rule) => {
    setEditingRule(rule);
    setFormData({
      scope_type: rule.scope_type,
      scope_ref: rule.scope_ref || '',
      ecosystem: rule.ecosystem || '',
      event: rule.event || '',
      tool_name_pattern: rule.tool_name_pattern || '',
      match_pattern: rule.match_pattern || '',
      decision: rule.decision,
      reason: rule.reason,
      enabled: rule.enabled,
      priority: rule.priority,
    });
    setShowForm(true);
  };

  const openCreateForm = () => {
    resetForm();
    setShowForm(true);
  };

  if (loading) {
    return <div className="loading">Loading hook rules...</div>;
  }

  return (
    <div className="hooks-rules-tab">
      <div className="tab-header">
        <p>
          Declarative rules the evaluation engine matches against every hook event, after the
          always-on Tier-1 denylist and before any opt-in remote policy check.
        </p>
        <button className="btn-primary" onClick={openCreateForm}>
          + New Rule
        </button>
      </div>

      {rules.length === 0 ? (
        <div className="empty-state">
          <p>No hook rules configured yet</p>
          <button className="btn-primary" onClick={openCreateForm}>
            Create First Rule
          </button>
        </div>
      ) : (
        <div className="hooks-table">
          <table>
            <thead>
              <tr>
                <th>Scope</th>
                <th>Matcher</th>
                <th>Decision</th>
                <th>Reason</th>
                <th>Priority</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((rule) => (
                <tr key={rule.id} data-testid="hook-rule-row">
                  <td>{scopeLabel(rule.scope_type, rule.scope_ref)}</td>
                  <td>
                    <div className="matcher-cell">
                      <span>{rule.ecosystem ? ECOSYSTEM_LABELS[rule.ecosystem] : 'Any ecosystem'}</span>
                      <span>{rule.event ? EVENT_LABELS[rule.event] : 'Any event'}</span>
                      {rule.tool_name_pattern && <code>{rule.tool_name_pattern}</code>}
                      {rule.match_pattern && <code>{rule.match_pattern}</code>}
                    </div>
                  </td>
                  <td>
                    <span className={`status-badge ${DECISION_BADGE_CLASS[rule.decision]}`}>
                      {DECISION_LABELS[rule.decision]}
                    </span>
                  </td>
                  <td className="reason-cell">{rule.reason}</td>
                  <td>{rule.priority}</td>
                  <td>
                    <span className={`status-badge ${rule.enabled ? 'active' : 'disabled'}`}>
                      {rule.enabled ? 'Enabled' : 'Disabled'}
                    </span>
                  </td>
                  <td>
                    {isOwnRow(rule) ? (
                      <div className="action-buttons">
                        <button
                          className="btn-small btn-secondary"
                          onClick={() => handleToggleEnabled(rule)}
                        >
                          {rule.enabled ? 'Disable' : 'Enable'}
                        </button>
                        <button className="btn-small btn-secondary" onClick={() => openEditForm(rule)}>
                          Edit
                        </button>
                        <button className="btn-small btn-danger" onClick={() => handleDelete(rule)}>
                          Delete
                        </button>
                      </div>
                    ) : (
                      <span className="limit-text">Read-only (global)</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showForm && (
        <HookRuleModal
          isAdmin={isAdmin}
          editingRule={editingRule}
          formData={formData}
          setFormData={setFormData}
          onSubmit={handleSubmit}
          onCancel={closeModal}
        />
      )}
    </div>
  );
}

export default HookRulesTab;
