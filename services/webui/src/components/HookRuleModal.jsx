import {
  HOOK_ECOSYSTEMS,
  ECOSYSTEM_LABELS,
  HOOK_EVENTS,
  EVENT_LABELS,
  HOOK_DECISIONS,
  DECISION_LABELS,
} from './hooksConstants';

// Create/edit form for one `hook_rules` row. Split out of HookRulesTab.jsx to
// keep that file under the project's 25,000-character limit. Owns no
// persistence -- the parent supplies formData/setFormData and onSubmit,
// which decides POST vs PUT and runs the "this is a live DENY rule" confirm
// before ever calling the API.
function HookRuleModal({ isAdmin, editingRule, formData, setFormData, onSubmit, onCancel }) {
  const setField = (field, value) => setFormData({ ...formData, [field]: value });

  return (
    <div className="modal" data-testid="hook-rule-modal">
      <div className="modal-content">
        <h2>{editingRule ? 'Edit Hook Rule' : 'New Hook Rule'}</h2>
        <form onSubmit={onSubmit}>
          {isAdmin ? (
            <div className="form-group">
              <label htmlFor="rule-scope-type">Scope</label>
              <select
                id="rule-scope-type"
                value={formData.scope_type}
                onChange={(e) => setField('scope_type', e.target.value)}
                disabled={!!editingRule}
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
                  onChange={(e) => setField('scope_ref', e.target.value)}
                  disabled={!!editingRule}
                  required
                />
              )}
              {editingRule && <small>Scope cannot be changed after creation.</small>}
            </div>
          ) : (
            <div className="form-group">
              <label>Scope</label>
              <p className="scope-notice">This rule applies to your organization only.</p>
            </div>
          )}

          <div className="form-row">
            <div className="form-group">
              <label htmlFor="rule-ecosystem">Ecosystem</label>
              <select
                id="rule-ecosystem"
                value={formData.ecosystem}
                onChange={(e) => setField('ecosystem', e.target.value)}
              >
                <option value="">Any ecosystem</option>
                {HOOK_ECOSYSTEMS.map((eco) => (
                  <option key={eco} value={eco}>
                    {ECOSYSTEM_LABELS[eco] || eco}
                  </option>
                ))}
              </select>
            </div>
            <div className="form-group">
              <label htmlFor="rule-event">Event</label>
              <select
                id="rule-event"
                value={formData.event}
                onChange={(e) => setField('event', e.target.value)}
              >
                <option value="">Any event</option>
                {HOOK_EVENTS.map((evt) => (
                  <option key={evt} value={evt}>
                    {EVENT_LABELS[evt] || evt}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="form-group">
            <label htmlFor="rule-tool-pattern">Tool Name Pattern</label>
            <input
              id="rule-tool-pattern"
              type="text"
              value={formData.tool_name_pattern}
              onChange={(e) => setField('tool_name_pattern', e.target.value)}
              placeholder="e.g. Bash, Edit, * (leave empty to match any tool)"
            />
          </div>

          <div className="form-group">
            <label htmlFor="rule-match-pattern">Path / Command Pattern</label>
            <input
              id="rule-match-pattern"
              type="text"
              value={formData.match_pattern}
              onChange={(e) => setField('match_pattern', e.target.value)}
              placeholder="e.g. *.tfstate, rm -rf * (leave empty to match any input)"
            />
            <small>Matched against the tool&apos;s flattened path/command text.</small>
          </div>

          <div className="form-group">
            <label htmlFor="rule-decision">Decision</label>
            <select
              id="rule-decision"
              value={formData.decision}
              onChange={(e) => setField('decision', e.target.value)}
            >
              {HOOK_DECISIONS.map((d) => (
                <option key={d} value={d}>
                  {DECISION_LABELS[d]}
                </option>
              ))}
            </select>
            {formData.decision === 'deny' && (
              <small className="warning-text">
                A Deny rule blocks the matching tool call outright for every developer in scope.
              </small>
            )}
          </div>

          <div className="form-group">
            <label htmlFor="rule-reason">Reason (shown to the developer)</label>
            <textarea
              id="rule-reason"
              rows="3"
              value={formData.reason}
              onChange={(e) => setField('reason', e.target.value)}
              placeholder="Explain why this rule exists so a blocked developer knows what to do next"
              required
            />
          </div>

          <div className="form-row">
            <div className="form-group">
              <label htmlFor="rule-priority">Priority</label>
              <input
                id="rule-priority"
                type="number"
                value={formData.priority}
                onChange={(e) => setField('priority', e.target.value)}
              />
            </div>
            <div className="form-group checkbox-group">
              <label htmlFor="rule-enabled">
                <input
                  id="rule-enabled"
                  type="checkbox"
                  checked={formData.enabled}
                  onChange={(e) => setField('enabled', e.target.checked)}
                />
                Enabled
              </label>
            </div>
          </div>

          <div className="form-actions">
            <button type="submit" className="btn-primary">
              {editingRule ? 'Save Changes' : 'Create Rule'}
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

export default HookRuleModal;
