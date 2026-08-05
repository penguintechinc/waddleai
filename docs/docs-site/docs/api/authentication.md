# Authentication & Authorization

WaddleAI implements a comprehensive authentication and authorization system with role-based access control (RBAC) to secure both the proxy API and management portal.

## Authentication Methods

WaddleAI supports three authentication methods:

### 1. API Keys

API keys are the primary authentication method for programmatic access to the proxy API.

**Format:** `wa-{user_id}-{random_hex}`

**Example:** `wa-5-abc123def456`

**Usage:**
```bash
curl https://your-waddleai-proxy.com/v1/chat/completions \
  -H "Authorization: Bearer wa-5-abc123def456" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### 2. JWT Tokens

JWT (JSON Web Tokens) are used for web application authentication and short-lived session management.

**Obtaining a JWT Token:**
```bash
curl -X POST https://your-waddleai-mgmt.com/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "username": "john.doe",
    "password": "your-password"
  }'
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "user": {
    "id": 5,
    "username": "john.doe",
    "role": "user",
    "organization_id": 1
  }
}
```

**Using JWT Token:**
```bash
curl https://your-waddleai-mgmt.com/api/users \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
```

### 3. Session Cookies

Session cookies are automatically set by the management portal for web browser authentication.

**Login Flow:**
```javascript
// Frontend login
const response = await fetch('/auth/login', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  credentials: 'include',
  body: JSON.stringify({
    username: 'john.doe',
    password: 'password'
  })
});

// Cookie is automatically set
// Subsequent requests include cookie automatically
```

## Role-Based Access Control (RBAC)

WaddleAI implements four user roles with hierarchical permissions:

### Admin

**Full system access** - Complete control over all resources

**Permissions:**
- `admin:*` - All administrative operations
- Manage all organizations
- Create/modify/delete users across all organizations
- Configure LLM providers and connection links
- View system-wide analytics and logs
- Configure routing and Redis
- Manage MCP servers
- Control XDP performance settings
- Access all API keys and usage data

**Use Cases:**
- System administrators
- DevOps engineers
- Platform operators

**API Access Example:**
```python
# Admin can access all organizations
orgs = requests.get(
    "https://mgmt.waddleai.com/api/organizations",
    headers={"Authorization": f"Bearer {admin_token}"}
).json()

# Admin can configure system-wide settings
routing = requests.post(
    "https://mgmt.waddleai.com/api/routing/instructions",
    headers={"Authorization": f"Bearer {admin_token}"},
    json={
        "instructions": "Route complex queries to GPT-4...",
        "routing_llm": "llama3.2:1b"
    }
)
```

### Resource Manager

**Organization-scoped management** - Control quotas and users within assigned organizations

**Permissions:**
- `resource:manage` - Manage organization resources
- Create/modify users within assigned organizations
- Manage API keys for organization members
- Set and enforce token quotas
- View organization-level analytics
- Manage organization settings

**Restrictions:**
- Cannot access other organizations
- Cannot modify system-wide settings
- Cannot access LLM provider configuration
- Cannot view system health metrics

**Use Cases:**
- Department managers
- Team leads
- Organization administrators

**API Access Example:**
```python
# Resource manager sees only their organization
users = requests.get(
    "https://mgmt.waddleai.com/api/users",
    headers={"Authorization": f"Bearer {resource_mgr_token}"}
).json()
# Returns only users in resource manager's organization

# Can update quotas for organization users
quota = requests.post(
    "https://mgmt.waddleai.com/analytics/quotas/user123",
    headers={"Authorization": f"Bearer {resource_mgr_token}"},
    json={"monthly_limit": 200000}
)
```

### Reporter

**Read-only analytics and reporting** - View usage data and security reports

**Permissions:**
- `report:view` - View analytics and reports
- Access organization-level usage statistics
- Generate usage reports
- View security incident logs
- Access memory/conversation analytics
- Monitor quota utilization

**Restrictions:**
- No write access to any resources
- Cannot create or modify users
- Cannot manage API keys
- Cannot change quotas or settings
- Limited to assigned organizations

**Use Cases:**
- Business analysts
- Compliance officers
- Security auditors
- Reporting teams

**API Access Example:**
```python
# Reporter can view organization analytics
analytics = requests.get(
    "https://mgmt.waddleai.com/api/usage?days=30",
    headers={"Authorization": f"Bearer {reporter_token}"}
).json()

# Can access security reports
security = requests.get(
    "https://mgmt.waddleai.com/analytics/security",
    headers={"Authorization": f"Bearer {reporter_token}"}
).json()

# Cannot create users (403 Forbidden)
new_user = requests.post(
    "https://mgmt.waddleai.com/api/users",
    headers={"Authorization": f"Bearer {reporter_token}"},
    json={"username": "test"}
)
# Returns: 403 Insufficient permissions
```

### User

**Basic API access** - Use proxy API with personal API keys

**Permissions:**
- `chat:completions` - Access OpenAI-compatible API
- View personal usage statistics
- Manage personal API keys
- Check personal quota status
- View personal conversation history

**Restrictions:**
- Cannot access management portal
- Cannot view other users' data
- Cannot modify organization settings
- Cannot view system configuration
- Limited to own resources

**Use Cases:**
- Application developers
- End users
- API consumers
- Application services

**API Access Example:**
```python
# User can use proxy API
response = openai.ChatCompletion.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello!"}],
    api_key="wa-5-abc123"  # User's API key
)

# User can check own usage
my_usage = requests.get(
    "https://proxy.waddleai.com/api/usage",
    headers={"Authorization": f"Bearer wa-5-abc123"}
).json()

# User can manage own API keys
new_key = requests.post(
    "https://mgmt.waddleai.com/api/api_keys",
    headers={"Authorization": f"Bearer {user_token}"},
    json={"name": "Production Key"}
)
```

## Permission Matrix

| Action | Admin | Resource Manager | Reporter | User |
|--------|-------|-----------------|----------|------|
| View all organizations | ✓ | ✗ | ✗ | ✗ |
| Manage assigned organizations | ✓ | ✓ | ✗ | ✗ |
| Create users (any org) | ✓ | ✗ | ✗ | ✗ |
| Create users (own org) | ✓ | ✓ | ✗ | ✗ |
| View all users | ✓ | ✗ | ✗ | ✗ |
| View org users | ✓ | ✓ | ✓ | ✗ |
| Manage API keys (any) | ✓ | ✗ | ✗ | ✗ |
| Manage API keys (org) | ✓ | ✓ | ✗ | ✗ |
| Manage API keys (own) | ✓ | ✓ | ✗ | ✓ |
| Configure LLM providers | ✓ | ✗ | ✗ | ✗ |
| Configure routing | ✓ | ✗ | ✗ | ✗ |
| View system analytics | ✓ | ✗ | ✗ | ✗ |
| View org analytics | ✓ | ✓ | ✓ | ✗ |
| View personal usage | ✓ | ✓ | ✓ | ✓ |
| Set quotas (any org) | ✓ | ✗ | ✗ | ✗ |
| Set quotas (own org) | ✓ | ✓ | ✗ | ✗ |
| Access memory config | ✓ | ✗ | ✓ | ✗ |
| Manage MCP servers | ✓ | ✗ | ✗ | ✗ |
| Configure XDP | ✓ | ✗ | ✗ | ✗ |
| Use proxy API | ✓ | ✓ | ✓ | ✓ |

## API Key Management

### Creating API Keys

**User Level:**
```python
# Users can create their own API keys
import requests

new_key = requests.post(
    "https://mgmt.waddleai.com/api/api_keys",
    headers={"Authorization": f"Bearer {jwt_token}"},
    json={
        "name": "Production Key",
        "expires_days": 365,
        "rate_limit": 1000
    }
).json()

print(f"New API Key: {new_key['api_key']}")
# Output: wa-5-abc123def456
# SAVE THIS - it won't be shown again!
```

**Organization Level (Resource Manager):**
```python
# Resource managers can create keys for org members
new_key = requests.post(
    "https://mgmt.waddleai.com/api/api_keys",
    headers={"Authorization": f"Bearer {resource_mgr_token}"},
    json={
        "name": "Team Member Key",
        "user_id": 15,  # Another user in the org
        "expires_days": 90
    }
).json()
```

**System Level (Admin):**
```python
# Admins can create keys for any user/org
new_key = requests.post(
    "https://mgmt.waddleai.com/api/api_keys",
    headers={"Authorization": f"Bearer {admin_token}"},
    json={
        "name": "Special Purpose Key",
        "user_id": 42,
        "organization_id": 5,
        "permissions": ["admin:*"],
        "expires_days": 30
    }
).json()
```

### API Key Security

**Best Practices:**

1. **Store securely**: Use environment variables or secret management systems
   ```bash
   export WADDLEAI_API_KEY="wa-5-abc123def456"
   ```

2. **Rotate regularly**: Replace API keys periodically
   ```python
   # Disable old key
   requests.delete(
       f"https://mgmt.waddleai.com/api/api_keys/{old_key_id}",
       headers={"Authorization": f"Bearer {jwt_token}"}
   )

   # Create new key
   new_key = requests.post(
       "https://mgmt.waddleai.com/api/api_keys",
       headers={"Authorization": f"Bearer {jwt_token}"},
       json={"name": "Production Key - Rotated"}
   ).json()
   ```

3. **Use minimum permissions**: Grant only required access
   ```python
   # Limited scope key
   key = requests.post(
       "https://mgmt.waddleai.com/api/api_keys",
       json={
           "name": "Read-only Analytics Key",
           "permissions": ["analytics:read"],
           "rate_limit": 100
       }
   ).json()
   ```

4. **Monitor usage**: Track API key activity
   ```python
   usage = requests.get(
       "https://mgmt.waddleai.com/api/usage",
       headers={"Authorization": f"Bearer {api_key}"}
   ).json()
   ```

5. **Set expiration**: Always set reasonable expiration dates
   ```python
   key = requests.post(
       "https://mgmt.waddleai.com/api/api_keys",
       json={
           "name": "Temporary Key",
           "expires_days": 7  # Expires in 1 week
       }
   ).json()
   ```

## JWT Token Lifecycle

### Token Generation

Tokens are generated during login:

```python
import requests

response = requests.post(
    "https://mgmt.waddleai.com/auth/login",
    json={
        "username": "john.doe",
        "password": "secure-password"
    }
)

token = response.json()["access_token"]
```

### Token Contents

JWT tokens contain:
- User ID
- Username
- Role
- Organization ID
- Issued at timestamp
- Expiration timestamp

**Decoded JWT Example:**
```json
{
  "user_id": 5,
  "username": "john.doe",
  "role": "user",
  "organization_id": 1,
  "iat": 1696014000,
  "exp": 1696100400
}
```

### Token Validation

Tokens are validated on every request:

```python
# Valid token - request succeeds
response = requests.get(
    "https://mgmt.waddleai.com/api/users",
    headers={"Authorization": f"Bearer {valid_token}"}
)
# Status: 200 OK

# Expired token - request fails
response = requests.get(
    "https://mgmt.waddleai.com/api/users",
    headers={"Authorization": f"Bearer {expired_token}"}
)
# Status: 401 Unauthorized
# Error: {"error": {"type": "authentication_required", "message": "Token expired"}}
```

### Token Refresh

WaddleAI uses a re-authentication flow rather than refresh tokens:

```python
def get_fresh_token(username, password):
    response = requests.post(
        "https://mgmt.waddleai.com/auth/login",
        json={"username": username, "password": password}
    )
    return response.json()["access_token"]

# Implement in your application
try:
    response = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    response.raise_for_status()
except requests.HTTPError as e:
    if e.response.status_code == 401:
        # Re-authenticate
        token = get_fresh_token(username, password)
        # Retry request
        response = requests.get(url, headers={"Authorization": f"Bearer {token}"})
```

## Security Considerations

### Password Security

WaddleAI uses bcrypt for password hashing:

```python
# Passwords are automatically hashed
# Never stored in plain text
user = requests.post(
    "https://mgmt.waddleai.com/api/users",
    headers={"Authorization": f"Bearer {admin_token}"},
    json={
        "username": "new.user",
        "password": "secure-password",  # Automatically hashed
        "email": "user@example.com"
    }
)
```

**Password Requirements:**
- Minimum 8 characters (recommended: 12+)
- Mix of uppercase, lowercase, numbers, symbols
- Not in common password lists

### API Key Hashing

API keys are hashed before storage:

```python
# Original key shown once: wa-5-abc123def456
# Stored in database as: bcrypt hash
# Cannot be recovered - only validated
```

### Rate Limiting

All authentication endpoints are rate-limited:

```
/auth/login: 5 attempts per minute per IP
/api/*: Based on API key configuration
```

### IP Whitelisting (Optional)

Configure IP restrictions for API keys:

```python
key = requests.post(
    "https://mgmt.waddleai.com/api/api_keys",
    headers={"Authorization": f"Bearer {admin_token}"},
    json={
        "name": "Production Key",
        "allowed_ips": ["203.0.113.0/24", "198.51.100.42"]
    }
).json()
```

### Audit Logging

All authentication events are logged:

```python
# View auth logs (admin only)
logs = requests.get(
    "https://mgmt.waddleai.com/api/audit/auth",
    headers={"Authorization": f"Bearer {admin_token}"},
    params={"days": 7}
).json()

# Log entries include:
# - Timestamp
# - Username/API key ID
# - IP address
# - Success/failure
# - User agent
```

## Multi-Organization Setup

WaddleAI supports multiple isolated organizations:

### Organization Isolation

```python
# Org 1 users cannot see Org 2 data
org1_token = authenticate("org1.user", "password")
org1_usage = requests.get(
    "https://mgmt.waddleai.com/api/usage",
    headers={"Authorization": f"Bearer {org1_token}"}
).json()
# Returns only Org 1 usage

org2_token = authenticate("org2.user", "password")
org2_usage = requests.get(
    "https://mgmt.waddleai.com/api/usage",
    headers={"Authorization": f"Bearer {org2_token}"}
).json()
# Returns only Org 2 usage
```

### Cross-Organization Management

Only admins can manage multiple organizations:

```python
# Admin can view/manage all orgs
admin_token = authenticate("admin", "admin-password")

# Get all organizations
all_orgs = requests.get(
    "https://mgmt.waddleai.com/api/organizations",
    headers={"Authorization": f"Bearer {admin_token}"}
).json()

# Create new organization
new_org = requests.post(
    "https://mgmt.waddleai.com/api/organizations",
    headers={"Authorization": f"Bearer {admin_token}"},
    json={
        "name": "New Department",
        "token_quota_monthly": 1000000
    }
).json()
```

## Integration Examples

### Python SDK

```python
import requests
from typing import Optional

class WaddleAIClient:
    def __init__(self, base_url: str, api_key: Optional[str] = None):
        self.base_url = base_url
        self.api_key = api_key
        self.jwt_token = None

    def login(self, username: str, password: str):
        response = requests.post(
            f"{self.base_url}/auth/login",
            json={"username": username, "password": password}
        )
        response.raise_for_status()
        self.jwt_token = response.json()["access_token"]
        return self.jwt_token

    def _get_headers(self):
        if self.jwt_token:
            return {"Authorization": f"Bearer {self.jwt_token}"}
        elif self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        raise ValueError("No authentication configured")

    def get_users(self):
        response = requests.get(
            f"{self.base_url}/api/users",
            headers=self._get_headers()
        )
        response.raise_for_status()
        return response.json()

# Usage
client = WaddleAIClient("https://mgmt.waddleai.com")
client.login("admin", "password")
users = client.get_users()
```

### Node.js SDK

```javascript
class WaddleAIClient {
  constructor(baseUrl, apiKey = null) {
    this.baseUrl = baseUrl;
    this.apiKey = apiKey;
    this.jwtToken = null;
  }

  async login(username, password) {
    const response = await fetch(`${this.baseUrl}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password })
    });
    const data = await response.json();
    this.jwtToken = data.access_token;
    return this.jwtToken;
  }

  getHeaders() {
    if (this.jwtToken) {
      return { 'Authorization': `Bearer ${this.jwtToken}` };
    } else if (this.apiKey) {
      return { 'Authorization': `Bearer ${this.apiKey}` };
    }
    throw new Error('No authentication configured');
  }

  async getUsers() {
    const response = await fetch(`${this.baseUrl}/api/users`, {
      headers: this.getHeaders()
    });
    return response.json();
  }
}

// Usage
const client = new WaddleAIClient('https://mgmt.waddleai.com');
await client.login('admin', 'password');
const users = await client.getUsers();
```

## Troubleshooting

### Common Authentication Issues

**Issue: 401 Authentication Required**
```
Solution: Check token/API key is valid and not expired
```

**Issue: 403 Insufficient Permissions**
```
Solution: Verify user has required role for the endpoint
```

**Issue: Invalid API Key Format**
```
Solution: Ensure API key starts with "wa-" and matches format
```

**Issue: Token Expired**
```
Solution: Re-authenticate to get fresh JWT token
```

## Next Steps

- [Management API Reference](management-api.md)
- [API Examples](examples.md)
- [User Management Guide](../administration/user-management.md)
- [Security Best Practices](../administration/security-policies.md)