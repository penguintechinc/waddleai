// Shared constants for the Agent Hooks surface (spec §18), split out so
// HookRulesTab/HookRuleModal/HookDenylistTab/HookConfigTab/HookVisibilityTab
// all source the same option lists rather than duplicating them. Mirrors
// `services/management/app/api/v1/hooks.py::HOOK_ECOSYSTEMS/HOOK_EVENTS` and
// `hook_rules.py::_DECISIONS/_FAIL_MODES` -- keep in sync if those change.

export const HOOK_ECOSYSTEMS = ['claude-code', 'cortex', 'antigravity', 'vscode'];
export const ECOSYSTEM_LABELS = {
  'claude-code': 'Claude Code',
  cortex: 'Cortex',
  antigravity: 'Antigravity (AGY CLI)',
  vscode: 'VS Code',
};

export const HOOK_EVENTS = ['pre_tool_use', 'post_tool_use', 'session_start', 'notification'];
export const EVENT_LABELS = {
  pre_tool_use: 'Before Tool Use',
  post_tool_use: 'After Tool Use',
  session_start: 'Session Start',
  notification: 'Notification',
};

export const HOOK_DECISIONS = ['allow', 'deny', 'ask'];
export const DECISION_LABELS = { allow: 'Allow', deny: 'Deny', ask: 'Ask' };
export const DECISION_BADGE_CLASS = { allow: 'success', deny: 'error', ask: 'warning' };

export const REMOTE_EVAL_FAIL_MODES = ['open', 'closed'];

export const DEFAULT_RULE_FORM = {
  scope_type: 'org',
  scope_ref: '',
  ecosystem: '',
  event: '',
  tool_name_pattern: '',
  match_pattern: '',
  decision: 'deny',
  reason: '',
  enabled: true,
  priority: 100,
};

export const DEFAULT_CONFIG_FORM = {
  scope_type: 'org',
  scope_ref: '',
  remote_eval_enabled: false,
  remote_eval_timeout_ms: 200,
  remote_eval_fail_mode: 'open',
  capture_raw_payloads: false,
};

/** Human-readable "Global" / "Org #<ref>" label for a scope_type/scope_ref pair. */
export function scopeLabel(scopeType, scopeRef) {
  return scopeType === 'global' ? 'Global (all organizations)' : `Organization #${scopeRef}`;
}

/**
 * True when saving this rule/config needs the "this can block everyone"
 * confirmation -- an enabled `deny` decision, since that is the only
 * combination that can halt a developer's tool call outright.
 */
export function isDangerousRule(decision, enabled) {
  return decision === 'deny' && enabled;
}
