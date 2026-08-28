import { useState, useEffect, useCallback } from 'react';
import axios from 'axios';
import { DECISION_LABELS, DECISION_BADGE_CLASS, ECOSYSTEM_LABELS, EVENT_LABELS, scopeLabel } from './hooksConstants';

// Operator visibility over `GET /api/v1/hooks/metrics` (§18 telemetry/
// metrics surface). Answers, in order: is a rule actually firing and how
// often; what decisions come back broken down by ecosystem/event; what
// latency hooks add (p50/p95/p99 -- they run synchronously in the agent's
// loop); and how often the remote tier fails open vs closed. The
// `platform` section (everything except per-rule hit counts) is `null` for
// a non-admin caller -- see `hook_metrics.py` docstring for why that is
// deliberate, not a loading artifact, and rendered as an explicit notice
// rather than silently omitted so a resource_manager isn't left wondering
// why half the page is missing.
function HookVisibilityTab({ isAdmin, onError }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  const fetchMetrics = useCallback(async () => {
    try {
      setLoading(true);
      const response = await axios.get('/api/v1/hooks/metrics');
      setData(response.data.data);
    } catch (err) {
      onError(err.response?.data?.error || 'Failed to fetch hook metrics');
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => {
    fetchMetrics();
  }, [fetchMetrics]);

  if (loading) {
    return <div className="loading">Loading hook metrics...</div>;
  }
  if (!data) {
    return <div className="empty-state"><p>No metrics available</p></div>;
  }

  const { rule_hits: ruleHits, platform } = data;
  const latency = platform?.evaluation_latency;

  return (
    <div className="hooks-visibility-tab">
      <section className="hooks-config-card">
        <h3>Rule Hit Rates</h3>
        <p>Is a rule you authored actually firing, and how often -- versus one nobody ever hits?</p>
        {ruleHits.length === 0 ? (
          <div className="empty-state"><p>No hook rules to report on yet</p></div>
        ) : (
          <div className="hooks-table">
            <table>
              <thead>
                <tr>
                  <th>Scope</th>
                  <th>Decision</th>
                  <th>Matched</th>
                  <th>Allow</th>
                  <th>Deny</th>
                  <th>Ask</th>
                </tr>
              </thead>
              <tbody>
                {ruleHits.map((hit) => (
                  <tr key={hit.rule_id} data-testid="rule-hit-row">
                    <td>{scopeLabel(hit.scope_type, hit.scope_ref)}</td>
                    <td>
                      <span className={`status-badge ${DECISION_BADGE_CLASS[hit.decision]}`}>
                        {DECISION_LABELS[hit.decision]}
                      </span>
                    </td>
                    <td>
                      {hit.matched === 0 ? (
                        <span className="limit-text">Never matched</span>
                      ) : (
                        hit.matched
                      )}
                    </td>
                    <td>{hit.decided.allow}</td>
                    <td>{hit.decided.deny}</td>
                    <td>{hit.decided.ask}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {!isAdmin && (
        <div className="admin-required-notice" data-testid="platform-metrics-notice">
          Platform-wide decision breakdown, evaluation latency, and fail-open/fail-closed rates
          require Admin access -- these counters aren&apos;t scoped per organization, so they
          aren&apos;t shown here to avoid exposing other organizations&apos; aggregate activity.
        </div>
      )}

      {isAdmin && platform && (
        <>
          <section className="hooks-config-card">
            <h3>Evaluation Latency</h3>
            <p>Hooks run synchronously in the agent&apos;s loop -- this is the number that matters.</p>
            {latency ? (
              <div className="quick-stats">
                <div className="stat-card">
                  <span className="stat-label">p50</span>
                  <div className="stat-value">{latency.p50_ms}ms</div>
                </div>
                <div className="stat-card">
                  <span className="stat-label">p95</span>
                  <div className="stat-value">{latency.p95_ms}ms</div>
                </div>
                <div className="stat-card">
                  <span className="stat-label">p99</span>
                  <div className="stat-value">{latency.p99_ms}ms</div>
                </div>
                <div className="stat-card">
                  <span className="stat-label">Samples</span>
                  <div className="stat-value">{latency.sample_count}</div>
                </div>
              </div>
            ) : (
              <div className="empty-state"><p>No evaluations recorded yet</p></div>
            )}
          </section>

          <section className="hooks-config-card">
            <h3>Fail-Open vs Fail-Closed</h3>
            <p>
              A rise in fail-open is a silent security degradation: Tier-2 couldn&apos;t produce a
              verdict and the tool call was allowed anyway.
            </p>
            <div className="quick-stats">
              <div className="stat-card">
                <span className="stat-label">Fail Open</span>
                <div className="stat-value">{platform.fail_mode.fail_open}</div>
              </div>
              <div className="stat-card">
                <span className="stat-label">Fail Closed</span>
                <div className="stat-value">{platform.fail_mode.fail_closed}</div>
              </div>
            </div>
          </section>

          <section className="hooks-config-card">
            <h3>Decisions by Ecosystem / Event</h3>
            {platform.invocations.length === 0 ? (
              <div className="empty-state"><p>No hook invocations recorded yet</p></div>
            ) : (
              <div className="hooks-table">
                <table>
                  <thead>
                    <tr>
                      <th>Ecosystem</th>
                      <th>Event</th>
                      <th>Decision</th>
                      <th>Count</th>
                    </tr>
                  </thead>
                  <tbody>
                    {platform.invocations.map((inv, idx) => (
                      <tr key={idx}>
                        <td>{ECOSYSTEM_LABELS[inv.ecosystem] || inv.ecosystem}</td>
                        <td>{EVENT_LABELS[inv.event] || inv.event}</td>
                        <td>
                          <span className={`status-badge ${DECISION_BADGE_CLASS[inv.decision]}`}>
                            {DECISION_LABELS[inv.decision] || inv.decision}
                          </span>
                        </td>
                        <td>{inv.count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

export default HookVisibilityTab;
