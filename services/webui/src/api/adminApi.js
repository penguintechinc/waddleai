/**
 * Typed (JSDoc) axios client for the Management API domains added to the
 * WebUI: Users & Auth, Organizations & Teams, Quotas & Pooled Quotas,
 * Security Policies / Cilium / Signing Keys, Knowledge Base, and
 * Cache Configs & Configuration-as-Code Manifests.
 */
import axios from 'axios';

const V1 = '/api/v1';

/**
 * Await a request and return `res.data`, normalising errors into an Error.
 * @param {Promise<{data:*}>} promise
 * @returns {Promise<*>}
 */
async function requestRaw(promise) {
  try {
    const res = await promise;
    return res?.data;
  } catch (err) {
    throw new Error(err?.response?.data?.error || err?.message || 'Request failed');
  }
}

/**
 * Await a request returning a `{status, data, meta}` envelope and unwrap `data`.
 * @param {Promise<{data:{data:*}}>} promise
 * @returns {Promise<*>}
 */
async function requestEnvelope(promise) {
  const body = await requestRaw(promise);
  return body?.data !== undefined ? body.data : body;
}

// ---------------------------------------------------------------------------
// Users & Self-Service Password
// ---------------------------------------------------------------------------

/** List users (paginated). */
export function listUsers(params) {
  return requestRaw(axios.get(`${V1}/users`, { params }));
}

/** Get user details by ID. */
export function getUser(userId) {
  return requestRaw(axios.get(`${V1}/users/${userId}`));
}

/** Create a new user. */
export function createUser(body) {
  return requestRaw(axios.post(`${V1}/users`, body));
}

/** Update user fields by ID. */
export function updateUser(userId, body) {
  return requestRaw(axios.put(`${V1}/users/${userId}`, body));
}

/** Soft-delete (disable) a user by ID. */
export function deleteUser(userId) {
  return requestRaw(axios.delete(`${V1}/users/${userId}`));
}

/** Re-enable a disabled user by ID. */
export function enableUser(userId) {
  return requestRaw(axios.post(`${V1}/users/${userId}/enable`));
}

/** DSAR right-of-access export for a user. */
export function exportUserData(userId) {
  return requestRaw(axios.get(`${V1}/users/${userId}/export`));
}

/** DSAR right-to-erasure (anonymize PII) for a user. */
export function eraseUserData(userId) {
  return requestRaw(axios.post(`${V1}/users/${userId}/erase`));
}

/** Change current authenticated user's password. */
export function changePassword(body) {
  return requestRaw(axios.post(`${V1}/auth/change-password`, body));
}

// ---------------------------------------------------------------------------
// Organizations & Teams
// ---------------------------------------------------------------------------

/** List organizations (paginated). */
export function listOrganizations(params) {
  return requestRaw(axios.get(`${V1}/organizations`, { params }));
}

/** Get organization details + statistics. */
export function getOrganization(orgId) {
  return requestRaw(axios.get(`${V1}/organizations/${orgId}`));
}

/** Create an organization. */
export function createOrganization(body) {
  return requestRaw(axios.post(`${V1}/organizations`, body));
}

/** Update an organization. */
export function updateOrganization(orgId, body) {
  return requestRaw(axios.put(`${V1}/organizations/${orgId}`, body));
}

/** Disable an organization. */
export function deleteOrganization(orgId) {
  return requestRaw(axios.delete(`${V1}/organizations/${orgId}`));
}

/** Get organization daily/monthly token & cost usage. */
export function getOrganizationUsage(orgId) {
  return requestRaw(axios.get(`${V1}/organizations/${orgId}/usage`));
}

/** List teams in caller's organization. */
export function listTeams(params) {
  return requestRaw(axios.get(`${V1}/teams`, { params }));
}

/** Get team details + member count + caller role. */
export function getTeam(teamId) {
  return requestRaw(axios.get(`${V1}/teams/${teamId}`));
}

/** Create a team in caller's organization. */
export function createTeam(body) {
  return requestRaw(axios.post(`${V1}/teams`, body));
}

/** Rename or enable/disable a team. */
export function updateTeam(teamId, body) {
  return requestRaw(axios.put(`${V1}/teams/${teamId}`, body));
}

/** Delete a team. */
export function deleteTeam(teamId) {
  return requestRaw(axios.delete(`${V1}/teams/${teamId}`));
}

/** List members of a team. */
export function listTeamMembers(teamId, params) {
  return requestRaw(axios.get(`${V1}/teams/${teamId}/members`, { params }));
}

/** Add a user to a team. */
export function addTeamMember(teamId, body) {
  return requestRaw(axios.post(`${V1}/teams/${teamId}/members`, body));
}

/** Update a member's team role. */
export function setTeamMemberRole(teamId, userId, body) {
  return requestRaw(axios.put(`${V1}/teams/${teamId}/members/${userId}`, body));
}

/** Remove a member from a team. */
export function removeTeamMember(teamId, userId) {
  return requestRaw(axios.delete(`${V1}/teams/${teamId}/members/${userId}`));
}

/** Transfer team ownership to an existing member. */
export function transferTeamOwnership(teamId, body) {
  return requestRaw(axios.post(`${V1}/teams/${teamId}/transfer-ownership`, body));
}

// ---------------------------------------------------------------------------
// Quotas & Pooled Quotas
// ---------------------------------------------------------------------------

/** List all legacy quota configurations (orgs, users, keys). */
export function listQuotas(params) {
  return requestRaw(axios.get(`${V1}/quotas`, { params }));
}

/** Update a user's token quotas. */
export function setUserQuota(userId, body) {
  return requestRaw(axios.put(`${V1}/quotas/user/${userId}`, body));
}

/** Update an organization's token quotas. */
export function setOrgQuota(orgId, body) {
  return requestRaw(axios.put(`${V1}/quotas/org/${orgId}`, body));
}

/** Update a virtual key's budget and rate-limit quotas. */
export function setKeyQuota(keyId, body) {
  return requestRaw(axios.put(`${V1}/quotas/key/${keyId}`, body));
}

/** Get current quota status for an entity ('key' | 'user' | 'org'). */
export function getQuotaStatus(entityId, type = 'key') {
  return requestRaw(axios.get(`${V1}/quotas/status/${entityId}`, { params: { type } }));
}

/** Get caller's organization pool configuration & current cycle. */
export function getOrgPool() {
  return requestRaw(axios.get(`${V1}/pooled-quota/org-pool`));
}

/** Update caller's organization pool configuration. */
export function putOrgPool(body) {
  return requestRaw(axios.put(`${V1}/pooled-quota/org-pool`, body));
}

/** Allocate pool credits to a team. */
export function putTeamPool(teamId, body) {
  return requestRaw(axios.put(`${V1}/pooled-quota/teams/${teamId}/pool`, body));
}

/** List pooled-quota hard-cap overrides. */
export function listQuotaOverrides(params) {
  return requestRaw(axios.get(`${V1}/pooled-quota/overrides`, { params }));
}

/** Create a pooled-quota hard-cap override. */
export function createQuotaOverride(body) {
  return requestRaw(axios.post(`${V1}/pooled-quota/overrides`, body));
}

/** Delete a pooled-quota override. */
export function deleteQuotaOverride(overrideId) {
  return requestRaw(axios.delete(`${V1}/pooled-quota/overrides/${overrideId}`));
}

/** List global token-to-credit conversion rates. */
export function listConversionRates(params) {
  return requestRaw(axios.get(`${V1}/pooled-quota/conversion-rates`, { params }));
}

/** Upsert a token-to-credit conversion rate. */
export function putConversionRate(body) {
  return requestRaw(axios.put(`${V1}/pooled-quota/conversion-rates`, body));
}

/** Get per-scope pooled-quota ledger usage for a cycle. */
export function getPooledQuotaUsage(params) {
  return requestRaw(axios.get(`${V1}/pooled-quota/usage`, { params }));
}

/** Rebuild Valkey pooled-quota counters from the durable ledger. */
export function rebuildPooledQuotaCounters(body = {}) {
  return requestRaw(axios.post(`${V1}/pooled-quota/rebuild`, body));
}

/** Get a team's pooled-quota status, buffer, and per-user reservations. */
export function getTeamPooledQuota(teamId, params) {
  return requestRaw(axios.get(`${V1}/teams/${teamId}/pooled-quota`, { params }));
}

/** Set a team's lendable shared buffer. */
export function putTeamBuffer(teamId, body) {
  return requestRaw(axios.put(`${V1}/teams/${teamId}/pooled-quota/buffer`, body));
}

/** Upsert a team member's guaranteed credit reservation. */
export function putTeamReservation(teamId, userId, body) {
  return requestRaw(axios.put(`${V1}/teams/${teamId}/pooled-quota/reservations/${userId}`, body));
}

/** Delete a team member's guaranteed credit reservation. */
export function deleteTeamReservation(teamId, userId) {
  return requestRaw(axios.delete(`${V1}/teams/${teamId}/pooled-quota/reservations/${userId}`));
}

// ---------------------------------------------------------------------------
// Security Policies, Bypass Grants, Cilium & Signing Keys
// ---------------------------------------------------------------------------

/** List security policies (optionally filtered by scope_type / scope_ref). */
export function listSecurityPolicies(params) {
  return requestEnvelope(axios.get(`${V1}/security-policies/`, { params }));
}

/** Resolve effective security policy for org + model + tool + direction. */
export function resolveSecurityPolicy(params) {
  return requestEnvelope(axios.get(`${V1}/security-policies/resolve`, { params }));
}

/** Create or upsert a security policy by (scope_type, scope_ref, direction). */
export function upsertSecurityPolicy(body) {
  return requestEnvelope(axios.post(`${V1}/security-policies/`, body));
}

/** Update an existing security policy by ID. */
export function updateSecurityPolicy(policyId, body) {
  return requestEnvelope(axios.put(`${V1}/security-policies/${policyId}`, body));
}

/** Delete a security policy by ID. */
export function deleteSecurityPolicy(policyId) {
  return requestEnvelope(axios.delete(`${V1}/security-policies/${policyId}`));
}

/** List security bypass grants. */
export function listBypassGrants(params) {
  return requestEnvelope(axios.get(`${V1}/security-policies/bypass-grants`, { params }));
}

/** Create a time-bounded security bypass grant. */
export function createBypassGrant(body) {
  return requestEnvelope(axios.post(`${V1}/security-policies/bypass-grants`, body));
}

/** Revoke a security bypass grant by ID. */
export function revokeBypassGrant(grantId) {
  return requestEnvelope(axios.delete(`${V1}/security-policies/bypass-grants/${grantId}`));
}

/** Get Cilium CRD capability status and last reconcile outcome. */
export function getCiliumStatus() {
  return requestRaw(axios.get(`${V1}/cilium/status`));
}

/** Trigger an on-demand Cilium policy reconcile. */
export function reconcileCilium() {
  return requestRaw(axios.post(`${V1}/cilium/reconcile`));
}

/** Rotate the active OIDC signing key. */
export function rotateSigningKey() {
  return requestRaw(axios.post(`${V1}/system/signing-key/rotate`));
}

// ---------------------------------------------------------------------------
// Knowledge Base
// ---------------------------------------------------------------------------

/** List uploaded knowledge documents for the caller's org. */
export function listKnowledge(params) {
  return requestRaw(axios.get(`${V1}/knowledge`, { params }));
}

/** Get a single knowledge document by ID. */
export function getKnowledge(docId) {
  return requestRaw(axios.get(`${V1}/knowledge/${docId}`));
}

/** Upload a PDF, Markdown, or text file into the org knowledge base. */
export function uploadKnowledge(file) {
  const formData = new FormData();
  formData.append('file', file);
  return requestRaw(
    axios.post(`${V1}/knowledge`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  );
}

/** Delete a knowledge document by ID. */
export function deleteKnowledge(docId) {
  return requestRaw(axios.delete(`${V1}/knowledge/${docId}`));
}

// ---------------------------------------------------------------------------
// Cache Configs & Configuration-as-Code Manifests
// ---------------------------------------------------------------------------

/** List response cache configs. */
export function listCacheConfigs(params) {
  return requestEnvelope(axios.get(`${V1}/cache-configs`, { params }));
}

/** Get a single cache config by ID. */
export function getCacheConfig(configId) {
  return requestEnvelope(axios.get(`${V1}/cache-configs/${configId}`));
}

/** Create a cache config for a scope. */
export function createCacheConfig(body) {
  return requestEnvelope(axios.post(`${V1}/cache-configs`, body));
}

/** Update an existing cache config by ID. */
export function updateCacheConfig(configId, body) {
  return requestEnvelope(axios.put(`${V1}/cache-configs/${configId}`, body));
}

/** Delete a cache config by ID. */
export function deleteCacheConfig(configId) {
  return requestEnvelope(axios.delete(`${V1}/cache-configs/${configId}`));
}

/** Plan (dry_run: true) or apply (dry_run: false, plan_id) a DAC manifest. */
export function applyManifest(body) {
  return requestRaw(axios.post(`${V1}/manifests/apply`, body));
}
