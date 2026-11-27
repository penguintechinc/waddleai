import { useState, useEffect } from 'react';
import {
  listOrganizations,
  getOrganization,
  createOrganization,
  updateOrganization,
  deleteOrganization,
  getOrganizationUsage,
  listTeams,
  getTeam,
  createTeam,
  updateTeam,
  deleteTeam,
  listTeamMembers,
  addTeamMember,
  setTeamMemberRole,
  removeTeamMember,
  transferTeamOwnership,
} from '../api/adminApi';
import './OrganizationsAndTeams.css';

const EMPTY_ORG = {
  name: '',
  description: '',
  token_quota_daily: 100000,
  token_quota_monthly: 1000000,
  default_model: '',
};

function OrganizationsAndTeams() {
  const [activeTab, setActiveTab] = useState('organizations');
  const [orgs, setOrgs] = useState([]);
  const [teams, setTeams] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  // Organizations state
  const [showOrgForm, setShowOrgForm] = useState(false);
  const [newOrg, setNewOrg] = useState(EMPTY_ORG);
  const [editingOrg, setEditingOrg] = useState(null);
  const [orgDetail, setOrgDetail] = useState(null);
  const [orgUsage, setOrgUsage] = useState(null);

  // Teams state
  const [newTeamName, setNewTeamName] = useState('');
  const [editingTeam, setEditingTeam] = useState(null);
  const [selectedTeam, setSelectedTeam] = useState(null);
  const [teamMembers, setTeamMembers] = useState([]);
  const [newMember, setNewMember] = useState({ user_id: '', team_role: 'member' });
  const [transferUserId, setTransferUserId] = useState('');

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
      const [orgsRes, teamsRes] = await Promise.all([listOrganizations(), listTeams()]);
      setOrgs(orgsRes?.organizations || []);
      setTeams(teamsRes?.teams || []);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleCreateOrg = async (e) => {
    e.preventDefault();
    try {
      const payload = {
        name: newOrg.name,
        description: newOrg.description,
        token_quota_daily: parseInt(newOrg.token_quota_daily, 10),
        token_quota_monthly: parseInt(newOrg.token_quota_monthly, 10),
      };
      if (newOrg.default_model) payload.default_model = newOrg.default_model;
      await createOrganization(payload);
      setNewOrg(EMPTY_ORG);
      setShowOrgForm(false);
      setError(null);
      flashSuccess('Organization created');
      fetchAll();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleUpdateOrg = async (e) => {
    e.preventDefault();
    try {
      await updateOrganization(editingOrg.id, {
        name: editingOrg.name,
        description: editingOrg.description,
        token_quota_daily: parseInt(editingOrg.token_quota_daily, 10),
        token_quota_monthly: parseInt(editingOrg.token_quota_monthly, 10),
        default_model: editingOrg.default_model || null,
        enabled: editingOrg.enabled,
      });
      setEditingOrg(null);
      setError(null);
      flashSuccess('Organization updated');
      fetchAll();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleDeleteOrg = async (orgId) => {
    if (!window.confirm('Disable this organization?')) return;
    try {
      await deleteOrganization(orgId);
      setError(null);
      flashSuccess('Organization disabled');
      fetchAll();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleInspectOrg = async (orgId) => {
    try {
      const [detail, usage] = await Promise.all([
        getOrganization(orgId),
        getOrganizationUsage(orgId),
      ]);
      setOrgDetail(detail);
      setOrgUsage(usage);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleCreateTeam = async (e) => {
    e.preventDefault();
    try {
      await createTeam({ name: newTeamName });
      setNewTeamName('');
      setError(null);
      flashSuccess('Team created');
      fetchAll();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleUpdateTeam = async (e) => {
    e.preventDefault();
    try {
      await updateTeam(editingTeam.id, {
        name: editingTeam.name,
        enabled: editingTeam.enabled,
      });
      setEditingTeam(null);
      setError(null);
      flashSuccess('Team updated');
      fetchAll();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleDeleteTeam = async (teamId) => {
    if (!window.confirm('Delete this team?')) return;
    try {
      await deleteTeam(teamId);
      if (selectedTeam?.team?.id === teamId) {
        setSelectedTeam(null);
        setTeamMembers([]);
      }
      setError(null);
      flashSuccess('Team deleted');
      fetchAll();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleOpenTeam = async (teamId) => {
    try {
      const [detail, membersRes] = await Promise.all([
        getTeam(teamId),
        listTeamMembers(teamId),
      ]);
      setSelectedTeam(detail);
      setTeamMembers(membersRes?.members || []);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleAddMember = async (e) => {
    e.preventDefault();
    try {
      await addTeamMember(selectedTeam.team.id, {
        user_id: parseInt(newMember.user_id, 10),
        team_role: newMember.team_role,
      });
      setNewMember({ user_id: '', team_role: 'member' });
      setError(null);
      flashSuccess('Member added');
      handleOpenTeam(selectedTeam.team.id);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleChangeMemberRole = async (userId, teamRole) => {
    try {
      await setTeamMemberRole(selectedTeam.team.id, userId, { team_role: teamRole });
      setError(null);
      flashSuccess('Member role updated');
      handleOpenTeam(selectedTeam.team.id);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleRemoveMember = async (userId) => {
    try {
      await removeTeamMember(selectedTeam.team.id, userId);
      setError(null);
      flashSuccess('Member removed');
      handleOpenTeam(selectedTeam.team.id);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleTransferOwnership = async (e) => {
    e.preventDefault();
    try {
      await transferTeamOwnership(selectedTeam.team.id, {
        new_owner_user_id: parseInt(transferUserId, 10),
      });
      setTransferUserId('');
      setError(null);
      flashSuccess('Ownership transferred');
      handleOpenTeam(selectedTeam.team.id);
    } catch (err) {
      setError(err.message);
    }
  };

  if (loading) {
    return <div className="admin-loading">Loading organizations and teams...</div>;
  }

  return (
    <div className="orgs-teams-page">
      <div className="page-header">
        <h1>Organizations &amp; Teams</h1>
        <div className="tab-buttons">
          <button
            type="button"
            className={`tab-btn ${activeTab === 'organizations' ? 'active' : ''}`}
            onClick={() => setActiveTab('organizations')}
          >
            Organizations ({orgs.length})
          </button>
          <button
            type="button"
            className={`tab-btn ${activeTab === 'teams' ? 'active' : ''}`}
            onClick={() => setActiveTab('teams')}
          >
            Teams ({teams.length})
          </button>
        </div>
      </div>

      {error && <div className="alert alert-error">{error}</div>}
      {success && <div className="alert alert-success">{success}</div>}

      {activeTab === 'organizations' && (
        <section>
          <div className="section-toolbar">
            <button
              type="button"
              className="btn-primary"
              onClick={() => setShowOrgForm(!showOrgForm)}
            >
              {showOrgForm ? 'Cancel' : 'Create Organization'}
            </button>
          </div>

          {showOrgForm && (
            <form className="admin-form-card" onSubmit={handleCreateOrg}>
              <h2>New Organization</h2>
              <div className="form-grid">
                <label>
                  Name
                  <input
                    type="text"
                    required
                    value={newOrg.name}
                    onChange={(e) => setNewOrg({ ...newOrg, name: e.target.value })}
                  />
                </label>
                <label>
                  Description
                  <input
                    type="text"
                    value={newOrg.description}
                    onChange={(e) => setNewOrg({ ...newOrg, description: e.target.value })}
                  />
                </label>
                <label>
                  Daily Token Quota
                  <input
                    type="number"
                    value={newOrg.token_quota_daily}
                    onChange={(e) => setNewOrg({ ...newOrg, token_quota_daily: e.target.value })}
                  />
                </label>
                <label>
                  Monthly Token Quota
                  <input
                    type="number"
                    value={newOrg.token_quota_monthly}
                    onChange={(e) => setNewOrg({ ...newOrg, token_quota_monthly: e.target.value })}
                  />
                </label>
                <label>
                  Default Model
                  <input
                    type="text"
                    value={newOrg.default_model}
                    onChange={(e) => setNewOrg({ ...newOrg, default_model: e.target.value })}
                  />
                </label>
              </div>
              <div className="form-actions">
                <button type="submit" className="btn-primary">Create</button>
              </div>
            </form>
          )}

          {editingOrg && (
            <form className="admin-form-card" onSubmit={handleUpdateOrg}>
              <h2>Edit Organization #{editingOrg.id}</h2>
              <div className="form-grid">
                <label>
                  Name
                  <input
                    type="text"
                    required
                    value={editingOrg.name}
                    onChange={(e) => setEditingOrg({ ...editingOrg, name: e.target.value })}
                  />
                </label>
                <label>
                  Description
                  <input
                    type="text"
                    value={editingOrg.description || ''}
                    onChange={(e) => setEditingOrg({ ...editingOrg, description: e.target.value })}
                  />
                </label>
                <label>
                  Daily Token Quota
                  <input
                    type="number"
                    value={editingOrg.token_quota_daily ?? ''}
                    onChange={(e) => setEditingOrg({ ...editingOrg, token_quota_daily: e.target.value })}
                  />
                </label>
                <label>
                  Monthly Token Quota
                  <input
                    type="number"
                    value={editingOrg.token_quota_monthly ?? ''}
                    onChange={(e) => setEditingOrg({ ...editingOrg, token_quota_monthly: e.target.value })}
                  />
                </label>
                <label>
                  Default Model
                  <input
                    type="text"
                    value={editingOrg.default_model || ''}
                    onChange={(e) => setEditingOrg({ ...editingOrg, default_model: e.target.value })}
                  />
                </label>
              </div>
              <div className="form-actions">
                <button type="submit" className="btn-primary">Save</button>
                <button type="button" className="btn-secondary" onClick={() => setEditingOrg(null)}>
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
                  <th>Name</th>
                  <th>Description</th>
                  <th>Users</th>
                  <th>Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {orgs.length === 0 ? (
                  <tr>
                    <td colSpan="6" className="empty-cell">No organizations found</td>
                  </tr>
                ) : (
                  orgs.map((o) => (
                    <tr key={o.id}>
                      <td>{o.id}</td>
                      <td>{o.name}</td>
                      <td>{o.description || '-'}</td>
                      <td>{o.user_count}</td>
                      <td>
                        <span className={`badge ${o.enabled ? 'badge-enabled' : 'badge-disabled'}`}>
                          {o.enabled ? 'Enabled' : 'Disabled'}
                        </span>
                      </td>
                      <td className="actions-cell">
                        <button type="button" className="btn-sm" onClick={() => handleInspectOrg(o.id)}>
                          Usage
                        </button>
                        <button type="button" className="btn-sm" onClick={() => setEditingOrg({ ...o })}>
                          Edit
                        </button>
                        <button type="button" className="btn-sm btn-danger" onClick={() => handleDeleteOrg(o.id)}>
                          Disable
                        </button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {orgDetail && orgUsage && (
            <div className="admin-form-card">
              <div className="modal-header">
                <h2>Organization Usage: {orgDetail.name}</h2>
                <button
                  type="button"
                  className="btn-sm"
                  onClick={() => { setOrgDetail(null); setOrgUsage(null); }}
                >
                  Close
                </button>
              </div>
              <div className="stats-row">
                <div className="stat-box">
                  <span className="stat-label">Users</span>
                  <span className="stat-value">{orgDetail.statistics?.user_count ?? 0}</span>
                </div>
                <div className="stat-box">
                  <span className="stat-label">Virtual Keys</span>
                  <span className="stat-value">{orgDetail.statistics?.key_count ?? 0}</span>
                </div>
                <div className="stat-box">
                  <span className="stat-label">Daily Tokens</span>
                  <span className="stat-value">{orgUsage.usage?.daily?.tokens ?? 0}</span>
                </div>
                <div className="stat-box">
                  <span className="stat-label">Monthly Tokens</span>
                  <span className="stat-value">{orgUsage.usage?.monthly?.tokens ?? 0}</span>
                </div>
                <div className="stat-box">
                  <span className="stat-label">Monthly Cost</span>
                  <span className="stat-value">${(orgUsage.usage?.monthly?.cost_usd ?? 0).toFixed(4)}</span>
                </div>
              </div>
            </div>
          )}
        </section>
      )}

      {activeTab === 'teams' && (
        <section>
          <form className="admin-form-card inline-form" onSubmit={handleCreateTeam}>
            <input
              type="text"
              placeholder="New team name"
              required
              value={newTeamName}
              onChange={(e) => setNewTeamName(e.target.value)}
            />
            <button type="submit" className="btn-primary">Create Team</button>
          </form>

          {editingTeam && (
            <form className="admin-form-card inline-form" onSubmit={handleUpdateTeam}>
              <input
                type="text"
                required
                value={editingTeam.name}
                onChange={(e) => setEditingTeam({ ...editingTeam, name: e.target.value })}
              />
              <button type="submit" className="btn-primary">Save Team</button>
              <button type="button" className="btn-secondary" onClick={() => setEditingTeam(null)}>
                Cancel
              </button>
            </form>
          )}

          <div className="admin-table-card">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Name</th>
                  <th>Org ID</th>
                  <th>Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {teams.length === 0 ? (
                  <tr>
                    <td colSpan="5" className="empty-cell">No teams found</td>
                  </tr>
                ) : (
                  teams.map((t) => (
                    <tr key={t.id}>
                      <td>{t.id}</td>
                      <td>{t.name}</td>
                      <td>{t.organization_id}</td>
                      <td>
                        <span className={`badge ${t.enabled ? 'badge-enabled' : 'badge-disabled'}`}>
                          {t.enabled ? 'Enabled' : 'Disabled'}
                        </span>
                      </td>
                      <td className="actions-cell">
                        <button type="button" className="btn-sm" onClick={() => handleOpenTeam(t.id)}>
                          Members
                        </button>
                        <button type="button" className="btn-sm" onClick={() => setEditingTeam({ ...t })}>
                          Rename
                        </button>
                        <button type="button" className="btn-sm btn-danger" onClick={() => handleDeleteTeam(t.id)}>
                          Delete
                        </button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {selectedTeam && (
            <div className="admin-form-card">
              <div className="modal-header">
                <h2>
                  Team: {selectedTeam.team?.name} (Your role: {selectedTeam.your_team_role || 'org_admin'})
                </h2>
                <button type="button" className="btn-sm" onClick={() => setSelectedTeam(null)}>
                  Close
                </button>
              </div>

              <form className="inline-form" onSubmit={handleAddMember}>
                <input
                  type="number"
                  placeholder="User ID"
                  required
                  value={newMember.user_id}
                  onChange={(e) => setNewMember({ ...newMember, user_id: e.target.value })}
                />
                <select
                  value={newMember.team_role}
                  onChange={(e) => setNewMember({ ...newMember, team_role: e.target.value })}
                >
                  <option value="viewer">viewer</option>
                  <option value="member">member</option>
                  <option value="admin">admin</option>
                  <option value="owner">owner</option>
                </select>
                <button type="submit" className="btn-primary">Add Member</button>
              </form>

              <table className="members-table">
                <thead>
                  <tr>
                    <th>User ID</th>
                    <th>Role</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {teamMembers.length === 0 ? (
                    <tr>
                      <td colSpan="3" className="empty-cell">No members</td>
                    </tr>
                  ) : (
                    teamMembers.map((m) => (
                      <tr key={m.user_id}>
                        <td>{m.user_id}</td>
                        <td>
                          <select
                            aria-label={`Role for user ${m.user_id}`}
                            value={m.team_role}
                            onChange={(e) => handleChangeMemberRole(m.user_id, e.target.value)}
                          >
                            <option value="viewer">viewer</option>
                            <option value="member">member</option>
                            <option value="admin">admin</option>
                            <option value="owner">owner</option>
                          </select>
                        </td>
                        <td>
                          <button
                            type="button"
                            className="btn-sm btn-danger"
                            onClick={() => handleRemoveMember(m.user_id)}
                          >
                            Remove
                          </button>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>

              <form className="inline-form transfer-form" onSubmit={handleTransferOwnership}>
                <input
                  type="number"
                  placeholder="New Owner User ID"
                  required
                  value={transferUserId}
                  onChange={(e) => setTransferUserId(e.target.value)}
                />
                <button type="submit" className="btn-secondary">Transfer Ownership</button>
              </form>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

export default OrganizationsAndTeams;
