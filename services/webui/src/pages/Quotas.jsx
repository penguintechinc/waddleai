import { useState, useEffect } from 'react';
import {
  listQuotas,
  setUserQuota,
  setOrgQuota,
  setKeyQuota,
  getQuotaStatus,
  getOrgPool,
  putOrgPool,
  putTeamPool,
  listQuotaOverrides,
  createQuotaOverride,
  deleteQuotaOverride,
  listConversionRates,
  putConversionRate,
  getPooledQuotaUsage,
  rebuildPooledQuotaCounters,
  getTeamPooledQuota,
  putTeamBuffer,
  putTeamReservation,
  deleteTeamReservation,
} from '../api/adminApi';
import './Quotas.css';

function Quotas() {
  const [activeTab, setActiveTab] = useState('legacy');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  // Legacy quotas
  const [quotas, setQuotas] = useState([]);
  const [editingQuota, setEditingQuota] = useState(null);
  const [quotaStatus, setQuotaStatus] = useState(null);

  // Pooled quota
  const [orgPool, setOrgPool] = useState(null);
  const [orgPoolForm, setOrgPoolForm] = useState({
    cycle_anchor_date: '',
    pool_credits: 0,
    enabled: false,
  });
  const [teamPoolForm, setTeamPoolForm] = useState({ team_id: '', pool_credits: '' });
  const [overrides, setOverrides] = useState([]);
  const [overrideForm, setOverrideForm] = useState({
    user_id: '',
    model: '',
    provider: '',
    cap_credits: '',
  });
  const [rates, setRates] = useState([]);
  const [rateForm, setRateForm] = useState({
    provider: '',
    model: '',
    input_rate: '1000',
    output_rate: '500',
    base_cost_per_waddleai_token: '0.001',
    enabled: true,
  });
  const [pooledUsage, setPooledUsage] = useState(null);

  // Team pooled quota
  const [teamIdInput, setTeamIdInput] = useState('');
  const [teamQuota, setTeamQuota] = useState(null);
  const [bufferCredits, setBufferCredits] = useState('');
  const [reservationForm, setReservationForm] = useState({
    user_id: '',
    guaranteed_credits: '',
    daily_subcap: '',
  });

  useEffect(() => {
    fetchLegacyQuotas();
  }, []);

  const flashSuccess = (msg) => {
    setSuccess(msg);
    setTimeout(() => setSuccess(null), 3000);
  };

  const fetchLegacyQuotas = async () => {
    try {
      setLoading(true);
      const res = await listQuotas();
      setQuotas(res?.quotas || []);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const fetchPooledData = async () => {
    try {
      const [poolRes, ovRes, rateRes, usageRes] = await Promise.all([
        getOrgPool().catch(() => null),
        listQuotaOverrides().catch(() => ({ overrides: [] })),
        listConversionRates().catch(() => ({ rates: [] })),
        getPooledQuotaUsage().catch(() => null),
      ]);
      if (poolRes) {
        setOrgPool(poolRes);
        setOrgPoolForm({
          cycle_anchor_date: poolRes.cycle_anchor_date || '',
          pool_credits: poolRes.pool_credits ?? 0,
          enabled: Boolean(poolRes.enabled),
        });
      }
      setOverrides(ovRes?.overrides || []);
      setRates(rateRes?.rates || []);
      setPooledUsage(usageRes);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleTabChange = (tab) => {
    setActiveTab(tab);
    if (tab === 'pooled') {
      fetchPooledData();
    }
  };

  const handleSaveQuota = async (e) => {
    e.preventDefault();
    try {
      if (editingQuota.type === 'user') {
        await setUserQuota(editingQuota.id, {
          token_quota_daily: parseInt(editingQuota.token_quota_daily, 10),
          token_quota_monthly: parseInt(editingQuota.token_quota_monthly, 10),
        });
      } else if (editingQuota.type === 'organization') {
        await setOrgQuota(editingQuota.id, {
          token_quota_daily: parseInt(editingQuota.token_quota_daily, 10),
          token_quota_monthly: parseInt(editingQuota.token_quota_monthly, 10),
        });
      } else {
        await setKeyQuota(editingQuota.id, {
          budget_limit_daily: parseFloat(editingQuota.budget_limit_daily),
          budget_limit_monthly: parseFloat(editingQuota.budget_limit_monthly),
          tpm_limit: parseInt(editingQuota.tpm_limit, 10),
          rpm_limit: parseInt(editingQuota.rpm_limit, 10),
        });
      }
      setEditingQuota(null);
      setError(null);
      flashSuccess('Quota updated');
      fetchLegacyQuotas();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleCheckStatus = async (q) => {
    const type = q.type === 'organization' ? 'org' : q.type;
    try {
      const status = await getQuotaStatus(q.id, type);
      setQuotaStatus(status);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleSaveOrgPool = async (e) => {
    e.preventDefault();
    try {
      await putOrgPool({
        cycle_anchor_date: orgPoolForm.cycle_anchor_date || null,
        pool_credits: parseInt(orgPoolForm.pool_credits, 10),
        enabled: orgPoolForm.enabled,
      });
      setError(null);
      flashSuccess('Org pool updated');
      fetchPooledData();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleSetTeamPool = async (e) => {
    e.preventDefault();
    try {
      await putTeamPool(parseInt(teamPoolForm.team_id, 10), {
        pool_credits: parseInt(teamPoolForm.pool_credits, 10),
      });
      setTeamPoolForm({ team_id: '', pool_credits: '' });
      setError(null);
      flashSuccess('Team pool allocated');
      fetchPooledData();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleCreateOverride = async (e) => {
    e.preventDefault();
    try {
      await createQuotaOverride({
        user_id: overrideForm.user_id ? parseInt(overrideForm.user_id, 10) : null,
        model: overrideForm.model || null,
        provider: overrideForm.provider || null,
        cap_credits: parseInt(overrideForm.cap_credits, 10),
      });
      setOverrideForm({ user_id: '', model: '', provider: '', cap_credits: '' });
      setError(null);
      flashSuccess('Override created');
      fetchPooledData();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleDeleteOverride = async (id) => {
    try {
      await deleteQuotaOverride(id);
      setError(null);
      flashSuccess('Override deleted');
      fetchPooledData();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleSaveRate = async (e) => {
    e.preventDefault();
    try {
      await putConversionRate({
        provider: rateForm.provider,
        model: rateForm.model,
        input_rate: parseFloat(rateForm.input_rate),
        output_rate: parseFloat(rateForm.output_rate),
        base_cost_per_waddleai_token: parseFloat(rateForm.base_cost_per_waddleai_token),
        enabled: rateForm.enabled,
      });
      setError(null);
      flashSuccess('Conversion rate saved');
      fetchPooledData();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleRebuildCounters = async () => {
    try {
      const res = await rebuildPooledQuotaCounters({});
      setError(null);
      flashSuccess(`Restored ${res?.counters_restored ?? 0} counters for cycle ${res?.cycle}`);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleLoadTeamQuota = async (e) => {
    if (e) e.preventDefault();
    if (!teamIdInput) return;
    try {
      const data = await getTeamPooledQuota(parseInt(teamIdInput, 10));
      setTeamQuota(data);
      setBufferCredits(String(data?.shared_buffer_credits ?? 0));
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleSaveTeamBuffer = async (e) => {
    e.preventDefault();
    try {
      await putTeamBuffer(teamQuota.team_id, {
        shared_buffer_credits: parseInt(bufferCredits, 10),
      });
      setError(null);
      flashSuccess('Team buffer updated');
      handleLoadTeamQuota();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleSaveReservation = async (e) => {
    e.preventDefault();
    try {
      await putTeamReservation(teamQuota.team_id, parseInt(reservationForm.user_id, 10), {
        guaranteed_credits: parseInt(reservationForm.guaranteed_credits, 10),
        daily_subcap: reservationForm.daily_subcap
          ? parseInt(reservationForm.daily_subcap, 10)
          : null,
      });
      setReservationForm({ user_id: '', guaranteed_credits: '', daily_subcap: '' });
      setError(null);
      flashSuccess('Reservation updated');
      handleLoadTeamQuota();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleDeleteReservation = async (userId) => {
    try {
      await deleteTeamReservation(teamQuota.team_id, userId);
      setError(null);
      flashSuccess('Reservation deleted');
      handleLoadTeamQuota();
    } catch (err) {
      setError(err.message);
    }
  };

  if (loading) {
    return <div className="admin-loading">Loading quotas...</div>;
  }

  return (
    <div className="quotas-page">
      <div className="page-header">
        <h1>Quotas &amp; Pooled Credits</h1>
        <div className="tab-buttons">
          <button
            type="button"
            className={`tab-btn ${activeTab === 'legacy' ? 'active' : ''}`}
            onClick={() => handleTabChange('legacy')}
          >
            Entity Quotas
          </button>
          <button
            type="button"
            className={`tab-btn ${activeTab === 'pooled' ? 'active' : ''}`}
            onClick={() => handleTabChange('pooled')}
          >
            Pooled Quota &amp; Rates
          </button>
          <button
            type="button"
            className={`tab-btn ${activeTab === 'team' ? 'active' : ''}`}
            onClick={() => handleTabChange('team')}
          >
            Team Reservations
          </button>
        </div>
      </div>

      {error && <div className="alert alert-error">{error}</div>}
      {success && <div className="alert alert-success">{success}</div>}

      {activeTab === 'legacy' && (
        <section>
          {editingQuota && (
            <form className="admin-form-card" onSubmit={handleSaveQuota}>
              <h2>Edit {editingQuota.type} Quota: {editingQuota.name}</h2>
              <div className="form-grid">
                {editingQuota.type === 'key' ? (
                  <>
                    <label>
                      Daily Budget ($)
                      <input
                        type="number"
                        step="0.01"
                        value={editingQuota.budget_limit_daily ?? 0}
                        onChange={(e) =>
                          setEditingQuota({ ...editingQuota, budget_limit_daily: e.target.value })
                        }
                      />
                    </label>
                    <label>
                      Monthly Budget ($)
                      <input
                        type="number"
                        step="0.01"
                        value={editingQuota.budget_limit_monthly ?? 0}
                        onChange={(e) =>
                          setEditingQuota({ ...editingQuota, budget_limit_monthly: e.target.value })
                        }
                      />
                    </label>
                    <label>
                      TPM Limit
                      <input
                        type="number"
                        value={editingQuota.tpm_limit ?? 0}
                        onChange={(e) =>
                          setEditingQuota({ ...editingQuota, tpm_limit: e.target.value })
                        }
                      />
                    </label>
                    <label>
                      RPM Limit
                      <input
                        type="number"
                        value={editingQuota.rpm_limit ?? 0}
                        onChange={(e) =>
                          setEditingQuota({ ...editingQuota, rpm_limit: e.target.value })
                        }
                      />
                    </label>
                  </>
                ) : (
                  <>
                    <label>
                      Daily Token Quota
                      <input
                        type="number"
                        value={editingQuota.token_quota_daily ?? 0}
                        onChange={(e) =>
                          setEditingQuota({ ...editingQuota, token_quota_daily: e.target.value })
                        }
                      />
                    </label>
                    <label>
                      Monthly Token Quota
                      <input
                        type="number"
                        value={editingQuota.token_quota_monthly ?? 0}
                        onChange={(e) =>
                          setEditingQuota({ ...editingQuota, token_quota_monthly: e.target.value })
                        }
                      />
                    </label>
                  </>
                )}
              </div>
              <div className="form-actions">
                <button type="submit" className="btn-primary">Save</button>
                <button type="button" className="btn-secondary" onClick={() => setEditingQuota(null)}>
                  Cancel
                </button>
              </div>
            </form>
          )}

          <div className="admin-table-card">
            <table>
              <thead>
                <tr>
                  <th>Type</th>
                  <th>ID</th>
                  <th>Name</th>
                  <th>Limits</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {quotas.length === 0 ? (
                  <tr>
                    <td colSpan="5" className="empty-cell">No quotas configured</td>
                  </tr>
                ) : (
                  quotas.map((q) => (
                    <tr key={`${q.type}-${q.id}`}>
                      <td><span className="badge badge-role">{q.type}</span></td>
                      <td>{q.id}</td>
                      <td>{q.name}</td>
                      <td>
                        {q.type === 'key'
                          ? `Daily $${q.budget_limit_daily ?? '-'} / Monthly $${q.budget_limit_monthly ?? '-'}`
                          : `Daily ${q.token_quota_daily ?? '-'} / Monthly ${q.token_quota_monthly ?? '-'}`}
                      </td>
                      <td className="actions-cell">
                        <button type="button" className="btn-sm" onClick={() => setEditingQuota({ ...q })}>
                          Edit
                        </button>
                        <button type="button" className="btn-sm" onClick={() => handleCheckStatus(q)}>
                          Status
                        </button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {quotaStatus && (
            <div className="admin-form-card">
              <div className="modal-header">
                <h2>Quota Status: {quotaStatus.name} ({quotaStatus.type})</h2>
                <button type="button" className="btn-sm" onClick={() => setQuotaStatus(null)}>
                  Close
                </button>
              </div>
              <pre className="json-preview">{JSON.stringify(quotaStatus, null, 2)}</pre>
            </div>
          )}
        </section>
      )}

      {activeTab === 'pooled' && (
        <section>
          <form className="admin-form-card" onSubmit={handleSaveOrgPool}>
            <div className="modal-header">
              <h2>Organization Pool</h2>
              <button type="button" className="btn-secondary" onClick={handleRebuildCounters}>
                Rebuild Counters
              </button>
            </div>
            {orgPool && (
              <p className="pool-meta">
                Allocated to teams: {orgPool.allocated_to_teams} | Current cycle: {orgPool.current_cycle ?? 'None'}
              </p>
            )}
            <div className="form-grid">
              <label>
                Cycle Anchor Date (YYYY-MM-DD)
                <input
                  type="date"
                  value={orgPoolForm.cycle_anchor_date}
                  onChange={(e) =>
                    setOrgPoolForm({ ...orgPoolForm, cycle_anchor_date: e.target.value })
                  }
                />
              </label>
              <label>
                Pool Credits
                <input
                  type="number"
                  value={orgPoolForm.pool_credits}
                  onChange={(e) =>
                    setOrgPoolForm({ ...orgPoolForm, pool_credits: e.target.value })
                  }
                />
              </label>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={orgPoolForm.enabled}
                  onChange={(e) =>
                    setOrgPoolForm({ ...orgPoolForm, enabled: e.target.checked })
                  }
                />
                Enabled
              </label>
            </div>
            <div className="form-actions">
              <button type="submit" className="btn-primary">Save Org Pool</button>
            </div>
          </form>

          <form className="admin-form-card inline-form" onSubmit={handleSetTeamPool}>
            <h2>Allocate Team Pool</h2>
            <input
              type="number"
              placeholder="Team ID"
              required
              value={teamPoolForm.team_id}
              onChange={(e) => setTeamPoolForm({ ...teamPoolForm, team_id: e.target.value })}
            />
            <input
              type="number"
              placeholder="Pool Credits"
              required
              value={teamPoolForm.pool_credits}
              onChange={(e) => setTeamPoolForm({ ...teamPoolForm, pool_credits: e.target.value })}
            />
            <button type="submit" className="btn-primary">Set Team Pool</button>
          </form>

          <div className="admin-form-card">
            <h2>Hard-Cap Overrides</h2>
            <form className="inline-form" onSubmit={handleCreateOverride}>
              <input
                type="number"
                placeholder="User ID (optional)"
                value={overrideForm.user_id}
                onChange={(e) => setOverrideForm({ ...overrideForm, user_id: e.target.value })}
              />
              <input
                type="text"
                placeholder="Model (optional)"
                value={overrideForm.model}
                onChange={(e) => setOverrideForm({ ...overrideForm, model: e.target.value })}
              />
              <input
                type="text"
                placeholder="Provider (optional)"
                value={overrideForm.provider}
                onChange={(e) => setOverrideForm({ ...overrideForm, provider: e.target.value })}
              />
              <input
                type="number"
                placeholder="Cap Credits"
                required
                value={overrideForm.cap_credits}
                onChange={(e) => setOverrideForm({ ...overrideForm, cap_credits: e.target.value })}
              />
              <button type="submit" className="btn-primary">Add Override</button>
            </form>
            <table className="sub-table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>User</th>
                  <th>Model</th>
                  <th>Provider</th>
                  <th>Cap Credits</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {overrides.map((ov) => (
                  <tr key={ov.id}>
                    <td>{ov.id}</td>
                    <td>{ov.user_id ?? '*'}</td>
                    <td>{ov.model ?? '*'}</td>
                    <td>{ov.provider ?? '*'}</td>
                    <td>{ov.cap_credits}</td>
                    <td>
                      <button type="button" className="btn-sm btn-danger" onClick={() => handleDeleteOverride(ov.id)}>
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="admin-form-card">
            <h2>Conversion Rates</h2>
            <form className="form-grid" onSubmit={handleSaveRate}>
              <label>
                Provider
                <input
                  type="text"
                  required
                  value={rateForm.provider}
                  onChange={(e) => setRateForm({ ...rateForm, provider: e.target.value })}
                />
              </label>
              <label>
                Model
                <input
                  type="text"
                  required
                  value={rateForm.model}
                  onChange={(e) => setRateForm({ ...rateForm, model: e.target.value })}
                />
              </label>
              <label>
                Input Rate
                <input
                  type="number"
                  step="any"
                  required
                  value={rateForm.input_rate}
                  onChange={(e) => setRateForm({ ...rateForm, input_rate: e.target.value })}
                />
              </label>
              <label>
                Output Rate
                <input
                  type="number"
                  step="any"
                  required
                  value={rateForm.output_rate}
                  onChange={(e) => setRateForm({ ...rateForm, output_rate: e.target.value })}
                />
              </label>
              <label>
                Base Cost / Token
                <input
                  type="number"
                  step="any"
                  required
                  value={rateForm.base_cost_per_waddleai_token}
                  onChange={(e) =>
                    setRateForm({ ...rateForm, base_cost_per_waddleai_token: e.target.value })
                  }
                />
              </label>
              <div className="form-actions">
                <button type="submit" className="btn-primary">Save Rate</button>
              </div>
            </form>
            <table className="sub-table">
              <thead>
                <tr>
                  <th>Provider</th>
                  <th>Model</th>
                  <th>Input Rate</th>
                  <th>Output Rate</th>
                  <th>Base Cost</th>
                </tr>
              </thead>
              <tbody>
                {rates.map((r) => (
                  <tr key={r.id}>
                    <td>{r.provider}</td>
                    <td>{r.model}</td>
                    <td>{r.input_rate}</td>
                    <td>{r.output_rate}</td>
                    <td>{r.base_cost_per_waddleai_token}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {pooledUsage && (
            <div className="admin-form-card">
              <h2>Cycle Usage (Cycle {pooledUsage.cycle ?? 'N/A'})</h2>
              <pre className="json-preview">{JSON.stringify(pooledUsage.entries || [], null, 2)}</pre>
            </div>
          )}
        </section>
      )}

      {activeTab === 'team' && (
        <section>
          <form className="admin-form-card inline-form" onSubmit={handleLoadTeamQuota}>
            <input
              type="number"
              placeholder="Team ID"
              required
              value={teamIdInput}
              onChange={(e) => setTeamIdInput(e.target.value)}
            />
            <button type="submit" className="btn-primary">Load Team Quota</button>
          </form>

          {teamQuota && (
            <div className="admin-form-card">
              <h2>Team #{teamQuota.team_id} Pool &amp; Reservations</h2>
              <p className="pool-meta">
                Pool: {teamQuota.pool_credits} | Buffer: {teamQuota.shared_buffer_credits} | Reserved:{' '}
                {teamQuota.reserved_credits} | Unallocated: {teamQuota.unallocated_credits}
              </p>

              <form className="inline-form" onSubmit={handleSaveTeamBuffer}>
                <input
                  type="number"
                  placeholder="Shared Buffer Credits"
                  required
                  value={bufferCredits}
                  onChange={(e) => setBufferCredits(e.target.value)}
                />
                <button type="submit" className="btn-secondary">Update Buffer</button>
              </form>

              <form className="inline-form reservation-form" onSubmit={handleSaveReservation}>
                <input
                  type="number"
                  placeholder="User ID"
                  required
                  value={reservationForm.user_id}
                  onChange={(e) =>
                    setReservationForm({ ...reservationForm, user_id: e.target.value })
                  }
                />
                <input
                  type="number"
                  placeholder="Guaranteed Credits"
                  required
                  value={reservationForm.guaranteed_credits}
                  onChange={(e) =>
                    setReservationForm({ ...reservationForm, guaranteed_credits: e.target.value })
                  }
                />
                <input
                  type="number"
                  placeholder="Daily Subcap (optional)"
                  value={reservationForm.daily_subcap}
                  onChange={(e) =>
                    setReservationForm({ ...reservationForm, daily_subcap: e.target.value })
                  }
                />
                <button type="submit" className="btn-primary">Save Reservation</button>
              </form>

              <table className="sub-table">
                <thead>
                  <tr>
                    <th>User ID</th>
                    <th>Guaranteed Credits</th>
                    <th>Daily Subcap</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {(teamQuota.reservations || []).map((r) => (
                    <tr key={r.user_id}>
                      <td>{r.user_id}</td>
                      <td>{r.guaranteed_credits}</td>
                      <td>{r.daily_subcap ?? 'None'}</td>
                      <td>
                        <button
                          type="button"
                          className="btn-sm btn-danger"
                          onClick={() => handleDeleteReservation(r.user_id)}
                        >
                          Remove
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

export default Quotas;
