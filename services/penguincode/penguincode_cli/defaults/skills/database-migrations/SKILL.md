---
name: database-migrations
description: "Schema changes, rollback procedures, and data integrity during migrations"
model: qwen2.5-coder:7b
---

# Database Migrations

## Overview
Manage database schema changes safely with versioned migrations, rollback support, and data integrity checks.

## Migration Workflow
1. **Create migration**: generate a new migration file
2. **Write up migration**: schema changes (add tables, columns, indexes)
3. **Write down migration**: rollback steps
4. **Test locally**: apply and rollback on dev database
5. **Review**: check migration in code review
6. **Apply**: run in staging, then production

## Best Practices
- **One logical change per migration** — don't combine unrelated changes
- **Always write rollback** — every `up` needs a `down`
- **Never modify existing migrations** — create new ones instead
- **Test rollback** — verify `down` migration works
- **Backup before production** — always backup before applying

## Common Operations
```sql
-- Add column (safe)
ALTER TABLE users ADD COLUMN phone VARCHAR(20);

-- Add index (non-blocking)
CREATE INDEX CONCURRENTLY idx_users_email ON users(email);

-- Rename column (requires app coordination)
ALTER TABLE users RENAME COLUMN name TO full_name;
```

## Dangerous Operations
- Dropping columns/tables — data loss risk
- Changing column types — may fail with existing data
- Adding NOT NULL without default — blocks on large tables
- Removing indexes — may cause query performance regression

## PyDAL Migrations
```python
# Define table changes in models
db.define_table('users',
    Field('email', 'string', unique=True),
    Field('phone', 'string'),  # New field
    migrate=True,
)
```
