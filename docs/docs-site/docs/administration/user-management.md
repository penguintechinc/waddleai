# User Management

Comprehensive guide to managing users, organizations, roles, and permissions in WaddleAI.

## Overview

WaddleAI implements a multi-tenant architecture with role-based access control (RBAC). Users belong to organizations and have assigned roles that determine their permissions.

## User Roles

WaddleAI supports four roles with increasing levels of access:

### User (Basic)
**Permissions**:
- Generate API keys
- Use API endpoints
- View own usage statistics
- View own conversation history

**Use case**: Developers and end-users who need API access

### Reporter
**Permissions**: All User permissions, plus:
- View analytics for their organization
- View memory/conversation data for their organization
- Export usage reports

**Use case**: Project managers, team leads who need visibility into usage

### Resource Manager
**Permissions**: All Reporter permissions, plus:
- Manage organization settings
- Create/manage users within their organization
- Configure LLM providers for their organization
- Set quotas for users in their organization
- Manage API keys for their organization

**Use case**: Organization administrators who manage resources and users

### Admin (Super User)
**Permissions**: All Resource Manager permissions, plus:
- Manage all organizations
- Manage all users across organizations
- Configure system-wide settings (routing, Redis, XDP, MCP)
- Access all analytics and logs
- Configure LLM providers globally

**Use case**: Platform administrators who manage the entire WaddleAI instance

## Creating Users

### Via Management Portal

1. Navigate to `http://localhost:8001/users` (Admin only)
2. Click **Create User**
3. Fill in user details:
   - Username (unique, alphanumeric + underscores)
   - Email address
   - Password (min 8 characters)
   - Organization (select from dropdown)
   - Role (User, Reporter, Resource Manager, Admin)
4. Click **Create**

### Via API

```bash
curl -X POST http://localhost:8001/api/users \
  -H "Authorization: Bearer <your-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "john_doe",
    "email": "john@example.com",
    "password": "SecurePass123!",
    "organization_id": 1,
    "role": "user"
  }'
```

Response:

```json
{
  "id": 42,
  "username": "john_doe",
  "email": "john@example.com",
  "organization_id": 1,
  "role": "user",
  "is_active": true,
  "created_at": "2024-01-15T10:30:00Z"
}
```

## Managing Organizations

### Create Organization

```bash
curl -X POST http://localhost:8001/api/organizations \
  -H "Authorization: Bearer <your-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Acme Corp",
    "description": "Acme Corporation development team",
    "quota_daily": 100000,
    "quota_monthly": 2000000,
    "default_model": "gpt-4"
  }'
```

### Update Organization Settings

```bash
curl -X PATCH http://localhost:8001/api/organizations/1 \
  -H "Authorization: Bearer <your-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "quota_daily": 150000,
    "default_model": "claude-3-opus"
  }'
```

### View Organization Users

```bash
curl http://localhost:8001/api/organizations/1/users \
  -H "Authorization: Bearer <your-admin-token>"
```

## User Quotas

### Individual User Quotas

Set quotas per user:

```bash
curl -X PATCH http://localhost:8001/api/users/42/quota \
  -H "Authorization: Bearer <your-resource-manager-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "daily_quota": 10000,
    "monthly_quota": 200000
  }'
```

**Quota hierarchy**:
1. User quota (if set) - Most restrictive
2. Organization quota - Falls back to this
3. System default - Last resort

### Check Quota Usage

```bash
curl http://localhost:8001/api/users/42/quota-usage \
  -H "Authorization: Bearer <your-token>"
```

Response:

```json
{
  "user_id": 42,
  "daily_quota": 10000,
  "daily_used": 3456,
  "daily_remaining": 6544,
  "daily_reset_at": "2024-01-16T00:00:00Z",
  "monthly_quota": 200000,
  "monthly_used": 87654,
  "monthly_remaining": 112346,
  "monthly_reset_at": "2024-02-01T00:00:00Z"
}
```

## User Authentication

### Password Requirements

- Minimum 8 characters
- At least one uppercase letter
- At least one lowercase letter
- At least one number
- Optional: Special characters

### Login

```bash
curl -X POST http://localhost:8001/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "username": "john_doe",
    "password": "SecurePass123!"
  }'
```

Response:

```json
{
  "access_token": "eyJhbGc...",
  "token_type": "bearer",
  "expires_in": 3600,
  "user": {
    "id": 42,
    "username": "john_doe",
    "role": "user",
    "organization_id": 1
  }
}
```

### Change Password

```bash
curl -X POST http://localhost:8001/auth/change-password \
  -H "Authorization: Bearer <your-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "current_password": "OldPass123!",
    "new_password": "NewPass456!"
  }'
```

### Reset Password (Admin)

```bash
curl -X POST http://localhost:8001/api/users/42/reset-password \
  -H "Authorization: Bearer <your-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "new_password": "ResetPass789!"
  }'
```

## API Key Management

Users can generate API keys for programmatic access.

### Generate API Key

```bash
curl -X POST http://localhost:8001/api/api-keys \
  -H "Authorization: Bearer <your-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Development Key",
    "default_model": "gpt-4",
    "daily_quota": 5000
  }'
```

Response:

```json
{
  "id": 123,
  "key": "wa-dev-abc123def456...",
  "name": "Development Key",
  "default_model": "gpt-4",
  "created_at": "2024-01-15T10:30:00Z",
  "expires_at": null
}
```

**⚠️ Important**: Save the API key immediately. It cannot be retrieved again.

### List API Keys

```bash
curl http://localhost:8001/api/api-keys \
  -H "Authorization: Bearer <your-token>"
```

### Revoke API Key

```bash
curl -X DELETE http://localhost:8001/api/api-keys/123 \
  -H "Authorization: Bearer <your-token>"
```

## User Activity Monitoring

### View User Activity

```bash
curl "http://localhost:8001/api/users/42/activity?limit=50" \
  -H "Authorization: Bearer <your-admin-token>"
```

Response:

```json
{
  "user_id": 42,
  "activities": [
    {
      "timestamp": "2024-01-15T10:30:00Z",
      "type": "api_request",
      "model": "gpt-4",
      "tokens": 150,
      "endpoint": "/v1/chat/completions"
    },
    {
      "timestamp": "2024-01-15T09:15:00Z",
      "type": "api_key_created",
      "key_id": 123
    }
  ],
  "total": 2
}
```

### Export User Data (GDPR)

```bash
curl http://localhost:8001/api/users/42/export \
  -H "Authorization: Bearer <your-token>"
```

Returns ZIP file with:
- User profile
- API keys (hashed)
- Usage history
- Conversations (if mem0 enabled)

## Batch Operations

### Bulk User Creation

```bash
curl -X POST http://localhost:8001/api/users/bulk \
  -H "Authorization: Bearer <your-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "users": [
      {
        "username": "user1",
        "email": "user1@example.com",
        "password": "Pass123!",
        "organization_id": 1,
        "role": "user"
      },
      {
        "username": "user2",
        "email": "user2@example.com",
        "password": "Pass456!",
        "organization_id": 1,
        "role": "user"
      }
    ]
  }'
```

### Bulk User Deactivation

```bash
curl -X POST http://localhost:8001/api/users/bulk-deactivate \
  -H "Authorization: Bearer <your-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "user_ids": [42, 43, 44]
  }'
```

## Security Best Practices

1. **Use strong passwords**: Enforce password policy in organization settings
2. **Rotate API keys**: Regularly rotate keys, especially for production
3. **Principle of least privilege**: Assign minimum required role
4. **Monitor activity**: Review user activity logs regularly
5. **Set quotas**: Prevent runaway usage with appropriate quotas
6. **Audit access**: Regular security audits of user permissions
7. **Deactivate unused accounts**: Remove or deactivate inactive users

## See Also

- [Organization Setup](organization-setup.md)
- [Quota Management](quota-management.md)
- [Security Policies](security-policies.md)
- [API Authentication](../api/authentication.md)
