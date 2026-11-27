import { useState, useEffect } from 'react';
import {
  listUsers,
  getUser,
  createUser,
  updateUser,
  deleteUser,
  enableUser,
  exportUserData,
  eraseUserData,
  changePassword,
} from '../api/adminApi';
import './Users.css';

const EMPTY_CREATE = {
  username: '',
  email: '',
  password: '',
  role: 'user',
  organization_id: '',
  token_quota_daily: 10000,
  token_quota_monthly: 100000,
  default_model: '',
};

function Users() {
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  const [showCreateForm, setShowCreateForm] = useState(false);
  const [newUser, setNewUser] = useState(EMPTY_CREATE);

  const [editingUser, setEditingUser] = useState(null);
  const [editForm, setEditForm] = useState({});

  const [selectedUser, setSelectedUser] = useState(null);
  const [dsarExport, setDsarExport] = useState(null);

  const [showPasswordModal, setShowPasswordModal] = useState(false);
  const [passwordForm, setPasswordForm] = useState({ current_password: '', new_password: '' });

  useEffect(() => {
    fetchUsers();
  }, []);

  const flashSuccess = (msg) => {
    setSuccess(msg);
    setTimeout(() => setSuccess(null), 3000);
  };

  const fetchUsers = async () => {
    try {
      setLoading(true);
      const data = await listUsers();
      setUsers(data?.users || []);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleCreateUser = async (e) => {
    e.preventDefault();
    try {
      const payload = {
        username: newUser.username,
        email: newUser.email,
        password: newUser.password,
        role: newUser.role,
        token_quota_daily: parseInt(newUser.token_quota_daily, 10),
        token_quota_monthly: parseInt(newUser.token_quota_monthly, 10),
      };
      if (newUser.organization_id !== '') {
        payload.organization_id = parseInt(newUser.organization_id, 10);
      }
      if (newUser.default_model) {
        payload.default_model = newUser.default_model;
      }
      await createUser(payload);
      setShowCreateForm(false);
      setNewUser(EMPTY_CREATE);
      setError(null);
      flashSuccess('User created successfully');
      fetchUsers();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleViewUser = async (userId) => {
    try {
      const detail = await getUser(userId);
      setSelectedUser(detail);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  };

  const startEdit = (user) => {
    setEditingUser(user);
    setEditForm({
      email: user.email || '',
      role: user.role || 'user',
      token_quota_daily: user.token_quota_daily ?? '',
      token_quota_monthly: user.token_quota_monthly ?? '',
      default_model: user.default_model || '',
      password: '',
    });
  };

  const handleUpdateUser = async (e) => {
    e.preventDefault();
    try {
      const payload = {
        email: editForm.email,
        role: editForm.role,
      };
      if (editForm.token_quota_daily !== '') {
        payload.token_quota_daily = parseInt(editForm.token_quota_daily, 10);
      }
      if (editForm.token_quota_monthly !== '') {
        payload.token_quota_monthly = parseInt(editForm.token_quota_monthly, 10);
      }
      if (editForm.default_model) {
        payload.default_model = editForm.default_model;
      }
      if (editForm.password) {
        payload.password = editForm.password;
      }
      await updateUser(editingUser.id, payload);
      setEditingUser(null);
      setError(null);
      flashSuccess('User updated successfully');
      fetchUsers();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleDisableUser = async (userId) => {
    if (!window.confirm('Disable this user account?')) return;
    try {
      await deleteUser(userId);
      setError(null);
      flashSuccess('User disabled');
      fetchUsers();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleEnableUser = async (userId) => {
    try {
      await enableUser(userId);
      setError(null);
      flashSuccess('User enabled');
      fetchUsers();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleExportUser = async (userId) => {
    try {
      const data = await exportUserData(userId);
      setDsarExport(data);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleEraseUser = async (userId) => {
    if (!window.confirm('Permanently anonymize PII for this user (GDPR Art. 17)? This cannot be undone.')) {
      return;
    }
    try {
      const res = await eraseUserData(userId);
      setError(null);
      flashSuccess(res?.message || 'User data erased');
      fetchUsers();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleChangePassword = async (e) => {
    e.preventDefault();
    try {
      await changePassword(passwordForm);
      setShowPasswordModal(false);
      setPasswordForm({ current_password: '', new_password: '' });
      setError(null);
      flashSuccess('Password changed successfully');
    } catch (err) {
      setError(err.message);
    }
  };

  if (loading) {
    return <div className="admin-loading">Loading users...</div>;
  }

  return (
    <div className="users-page">
      <div className="page-header">
        <h1>Users &amp; Identity</h1>
        <div className="header-actions">
          <button
            type="button"
            className="btn-secondary"
            onClick={() => setShowPasswordModal(true)}
          >
            Change Password
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={() => setShowCreateForm(!showCreateForm)}
          >
            {showCreateForm ? 'Cancel' : 'Create User'}
          </button>
        </div>
      </div>

      {error && <div className="alert alert-error">{error}</div>}
      {success && <div className="alert alert-success">{success}</div>}

      {showCreateForm && (
        <form className="admin-form-card" onSubmit={handleCreateUser}>
          <h2>New User</h2>
          <div className="form-grid">
            <label>
              Username
              <input
                type="text"
                required
                value={newUser.username}
                onChange={(e) => setNewUser({ ...newUser, username: e.target.value })}
              />
            </label>
            <label>
              Email
              <input
                type="email"
                required
                value={newUser.email}
                onChange={(e) => setNewUser({ ...newUser, email: e.target.value })}
              />
            </label>
            <label>
              Password
              <input
                type="password"
                required
                value={newUser.password}
                onChange={(e) => setNewUser({ ...newUser, password: e.target.value })}
              />
            </label>
            <label>
              Role
              <select
                value={newUser.role}
                onChange={(e) => setNewUser({ ...newUser, role: e.target.value })}
              >
                <option value="user">user</option>
                <option value="resource_manager">resource_manager</option>
                <option value="reporter">reporter</option>
                <option value="admin">admin</option>
              </select>
            </label>
            <label>
              Organization ID (optional)
              <input
                type="number"
                value={newUser.organization_id}
                onChange={(e) => setNewUser({ ...newUser, organization_id: e.target.value })}
              />
            </label>
            <label>
              Daily Token Quota
              <input
                type="number"
                value={newUser.token_quota_daily}
                onChange={(e) => setNewUser({ ...newUser, token_quota_daily: e.target.value })}
              />
            </label>
            <label>
              Monthly Token Quota
              <input
                type="number"
                value={newUser.token_quota_monthly}
                onChange={(e) => setNewUser({ ...newUser, token_quota_monthly: e.target.value })}
              />
            </label>
            <label>
              Default Model
              <input
                type="text"
                value={newUser.default_model}
                onChange={(e) => setNewUser({ ...newUser, default_model: e.target.value })}
              />
            </label>
          </div>
          <div className="form-actions">
            <button type="submit" className="btn-primary">Create</button>
          </div>
        </form>
      )}

      {editingUser && (
        <form className="admin-form-card" onSubmit={handleUpdateUser}>
          <h2>Edit User: {editingUser.username}</h2>
          <div className="form-grid">
            <label>
              Email
              <input
                type="email"
                value={editForm.email}
                onChange={(e) => setEditForm({ ...editForm, email: e.target.value })}
              />
            </label>
            <label>
              Role
              <select
                value={editForm.role}
                onChange={(e) => setEditForm({ ...editForm, role: e.target.value })}
              >
                <option value="user">user</option>
                <option value="resource_manager">resource_manager</option>
                <option value="reporter">reporter</option>
                <option value="admin">admin</option>
              </select>
            </label>
            <label>
              Daily Token Quota
              <input
                type="number"
                value={editForm.token_quota_daily}
                onChange={(e) => setEditForm({ ...editForm, token_quota_daily: e.target.value })}
              />
            </label>
            <label>
              Monthly Token Quota
              <input
                type="number"
                value={editForm.token_quota_monthly}
                onChange={(e) => setEditForm({ ...editForm, token_quota_monthly: e.target.value })}
              />
            </label>
            <label>
              Default Model
              <input
                type="text"
                value={editForm.default_model}
                onChange={(e) => setEditForm({ ...editForm, default_model: e.target.value })}
              />
            </label>
            <label>
              New Password (optional)
              <input
                type="password"
                value={editForm.password}
                onChange={(e) => setEditForm({ ...editForm, password: e.target.value })}
              />
            </label>
          </div>
          <div className="form-actions">
            <button type="submit" className="btn-primary">Save</button>
            <button type="button" className="btn-secondary" onClick={() => setEditingUser(null)}>
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
              <th>Username</th>
              <th>Email</th>
              <th>Role</th>
              <th>Org ID</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.length === 0 ? (
              <tr>
                <td colSpan="7" className="empty-cell">No users found</td>
              </tr>
            ) : (
              users.map((u) => (
                <tr key={u.id}>
                  <td>{u.id}</td>
                  <td>{u.username}</td>
                  <td>{u.email}</td>
                  <td><span className="badge badge-role">{u.role}</span></td>
                  <td>{u.organization_id}</td>
                  <td>
                    <span className={`badge ${u.enabled ? 'badge-enabled' : 'badge-disabled'}`}>
                      {u.enabled ? 'Enabled' : 'Disabled'}
                    </span>
                  </td>
                  <td className="actions-cell">
                    <button type="button" className="btn-sm" onClick={() => handleViewUser(u.id)}>
                      Details
                    </button>
                    <button type="button" className="btn-sm" onClick={() => startEdit(u)}>
                      Edit
                    </button>
                    {u.enabled ? (
                      <button type="button" className="btn-sm btn-warn" onClick={() => handleDisableUser(u.id)}>
                        Disable
                      </button>
                    ) : (
                      <button type="button" className="btn-sm btn-ok" onClick={() => handleEnableUser(u.id)}>
                        Enable
                      </button>
                    )}
                    <button type="button" className="btn-sm" onClick={() => handleExportUser(u.id)}>
                      DSAR Export
                    </button>
                    <button type="button" className="btn-sm btn-danger" onClick={() => handleEraseUser(u.id)}>
                      DSAR Erase
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {selectedUser && (
        <div className="modal-backdrop" role="dialog" aria-label="User Details">
          <div className="modal-card">
            <div className="modal-header">
              <h2>User Details: {selectedUser.username}</h2>
              <button type="button" className="btn-sm" onClick={() => setSelectedUser(null)}>Close</button>
            </div>
            <dl className="detail-list">
              <dt>ID</dt><dd>{selectedUser.id}</dd>
              <dt>Email</dt><dd>{selectedUser.email}</dd>
              <dt>Role</dt><dd>{selectedUser.role}</dd>
              <dt>Organization</dt><dd>{selectedUser.organization?.name || 'N/A'}</dd>
              <dt>Daily Quota</dt><dd>{selectedUser.token_quota_daily ?? 'N/A'}</dd>
              <dt>Monthly Quota</dt><dd>{selectedUser.token_quota_monthly ?? 'N/A'}</dd>
              <dt>Default Model</dt><dd>{selectedUser.default_model || 'N/A'}</dd>
              <dt>Login Count</dt><dd>{selectedUser.login_count ?? 0}</dd>
            </dl>
          </div>
        </div>
      )}

      {dsarExport && (
        <div className="modal-backdrop" role="dialog" aria-label="DSAR Export">
          <div className="modal-card">
            <div className="modal-header">
              <h2>DSAR Data Export (User #{dsarExport.subject_user_id})</h2>
              <button type="button" className="btn-sm" onClick={() => setDsarExport(null)}>Close</button>
            </div>
            <p className="dsar-notice">{dsarExport.notice}</p>
            <pre className="json-preview">{JSON.stringify(dsarExport, null, 2)}</pre>
          </div>
        </div>
      )}

      {showPasswordModal && (
        <div className="modal-backdrop" role="dialog" aria-label="Change Password">
          <form className="modal-card" onSubmit={handleChangePassword}>
            <div className="modal-header">
              <h2>Change Password</h2>
              <button type="button" className="btn-sm" onClick={() => setShowPasswordModal(false)}>
                Close
              </button>
            </div>
            <div className="form-stack">
              <label>
                Current Password
                <input
                  type="password"
                  required
                  value={passwordForm.current_password}
                  onChange={(e) =>
                    setPasswordForm({ ...passwordForm, current_password: e.target.value })
                  }
                />
              </label>
              <label>
                New Password (min 8 chars)
                <input
                  type="password"
                  required
                  minLength={8}
                  value={passwordForm.new_password}
                  onChange={(e) =>
                    setPasswordForm({ ...passwordForm, new_password: e.target.value })
                  }
                />
              </label>
            </div>
            <div className="form-actions">
              <button type="submit" className="btn-primary">Update Password</button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}

export default Users;
