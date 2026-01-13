"""
WaddleAI Management Server Extensions
Flask-Security-Too, PyDAL, Redis initialization
"""

import os
import logging
from datetime import datetime
from typing import Optional

from flask import Flask, current_app
from flask_security import Security, UserMixin, RoleMixin
from flask_security.datastore import UserDatastore
from pydal import DAL, Field
import redis

logger = logging.getLogger(__name__)

# Global instances
db: Optional[DAL] = None
security: Optional[Security] = None
redis_client: Optional[redis.Redis] = None


class PyDALUserDatastore(UserDatastore):
    """Custom UserDatastore for PyDAL integration with Flask-Security-Too"""

    def __init__(self, db: DAL):
        self.db = db

    def find_user(self, **kwargs):
        """Find user by any field"""
        query = None
        for key, value in kwargs.items():
            if key == 'id':
                key = 'id'
            condition = (self.db.users[key] == value)
            query = condition if query is None else (query & condition)

        if query is None:
            return None

        user_row = self.db(query).select().first()
        if user_row:
            return PyDALUser(user_row, self.db)
        return None

    def find_role(self, role):
        """Find role by name"""
        # Roles are stored as strings in user.role field
        return role

    def create_user(self, **kwargs):
        """Create a new user"""
        from passlib.hash import bcrypt
        password = kwargs.pop('password', None)
        if password:
            kwargs['password_hash'] = bcrypt.hash(password)

        kwargs['created_at'] = datetime.utcnow()
        user_id = self.db.users.insert(**kwargs)
        self.db.commit()
        return self.find_user(id=user_id)

    def delete_user(self, user):
        """Delete a user"""
        self.db(self.db.users.id == user.id).delete()
        self.db.commit()

    def add_role_to_user(self, user, role):
        """Add role to user"""
        self.db(self.db.users.id == user.id).update(role=role)
        self.db.commit()
        return True

    def remove_role_from_user(self, user, role):
        """Remove role from user"""
        self.db(self.db.users.id == user.id).update(role='user')
        self.db.commit()
        return True

    def toggle_active(self, user):
        """Toggle user active status"""
        new_status = not user.enabled
        self.db(self.db.users.id == user.id).update(enabled=new_status)
        self.db.commit()
        return new_status

    def deactivate_user(self, user):
        """Deactivate user"""
        self.db(self.db.users.id == user.id).update(enabled=False)
        self.db.commit()
        return True

    def activate_user(self, user):
        """Activate user"""
        self.db(self.db.users.id == user.id).update(enabled=True)
        self.db.commit()
        return True


class PyDALUser(UserMixin):
    """User model wrapper for PyDAL row with Flask-Security-Too compatibility"""

    def __init__(self, row, db: DAL):
        self._row = row
        self._db = db

    @property
    def id(self):
        return self._row.id

    @property
    def email(self):
        return self._row.email

    @property
    def username(self):
        return self._row.username

    @property
    def password(self):
        return self._row.password_hash

    @property
    def active(self):
        return self._row.enabled

    @property
    def fs_uniquifier(self):
        """Flask-Security unique identifier"""
        return str(self._row.id)

    @property
    def roles(self):
        """Return roles as list"""
        role = self._row.role
        if role:
            return [PyDALRole(role)]
        return []

    @property
    def organization_id(self):
        return self._row.organization_id

    def get_auth_token(self):
        """Return authentication token"""
        return None

    def has_role(self, role):
        """Check if user has role"""
        if isinstance(role, str):
            return self._row.role == role
        return self._row.role == role.name

    def verify_password(self, password):
        """Verify password"""
        from passlib.hash import bcrypt
        return bcrypt.verify(password, self._row.password_hash)


class PyDALRole(RoleMixin):
    """Role wrapper for Flask-Security-Too compatibility"""

    def __init__(self, name: str):
        self._name = name

    @property
    def name(self):
        return self._name

    def __eq__(self, other):
        if isinstance(other, str):
            return self._name == other
        if isinstance(other, PyDALRole):
            return self._name == other._name
        return False

    def __hash__(self):
        return hash(self._name)


def init_db(app: Flask) -> DAL:
    """Initialize PyDAL database connection"""
    global db

    db_url = app.config['DATABASE_URL']
    logger.info(f"Connecting to database: {db_url.split('@')[-1] if '@' in db_url else db_url}")

    db = DAL(db_url, migrate=True, fake_migrate_all=False, folder='databases')

    # Define tables
    define_tables(db)

    logger.info("Database initialized successfully")
    return db


def define_tables(db: DAL):
    """Define all database tables"""

    # Organizations for multi-tenancy
    db.define_table('organizations',
        Field('name', unique=True, required=True),
        Field('description', 'text'),
        Field('token_quota_monthly', 'integer', default=1000000),
        Field('token_quota_daily', 'integer', default=100000),
        Field('default_model', 'string'),
        Field('enabled', 'boolean', default=True),
        Field('created_at', 'datetime', default=datetime.utcnow),
        format='%(name)s'
    )

    # Users and Authentication
    db.define_table('users',
        Field('username', unique=True, required=True),
        Field('email', unique=True, required=True),
        Field('password_hash', 'password', required=True),
        Field('role', 'string', required=True, default='user'),
        Field('organization_id', 'reference organizations', required=True),
        Field('managed_orgs', 'list:reference organizations'),
        Field('created_at', 'datetime', default=datetime.utcnow),
        Field('token_quota_monthly', 'integer', default=100000),
        Field('token_quota_daily', 'integer', default=10000),
        Field('default_model', 'string'),
        Field('enabled', 'boolean', default=True),
        Field('last_login_at', 'datetime'),
        Field('current_login_at', 'datetime'),
        Field('last_login_ip', 'string'),
        Field('current_login_ip', 'string'),
        Field('login_count', 'integer', default=0),
        format='%(username)s'
    )

    # AI Providers (connection_links renamed and enhanced)
    db.define_table('ai_providers',
        Field('name', unique=True, required=True),
        Field('provider_type', 'string', required=True),  # openai, anthropic, ollama, gemini, bedrock, azure_openai, cohere
        Field('endpoint_url', required=True),
        Field('api_key', 'password'),
        Field('model_list', 'json'),
        Field('rate_limits', 'json'),
        Field('enabled', 'boolean', default=True),
        Field('tls_config', 'json'),
        Field('extra_config', 'json'),  # Provider-specific settings
        Field('priority', 'integer', default=100),
        Field('ailb_sync_enabled', 'boolean', default=True),
        Field('ailb_route_config', 'json'),
        Field('created_at', 'datetime', default=datetime.utcnow),
        format='%(name)s'
    )

    # MarchProxy AILB Sync Status
    db.define_table('marchproxy_ailb_sync',
        Field('provider_id', 'reference ai_providers'),
        Field('ailb_instance_id', 'string'),
        Field('ailb_route_id', 'string'),
        Field('sync_status', 'string', default='pending'),  # synced, pending, failed, deleted
        Field('last_synced', 'datetime'),
        Field('sync_error', 'text'),
        Field('config_hash', 'string'),
        Field('created_at', 'datetime', default=datetime.utcnow)
    )

    # Ollama Deployments
    db.define_table('ollama_deployments',
        Field('name', unique=True, required=True),
        Field('endpoint_url', required=True),
        Field('deployment_type', 'string', default='external'),  # docker, kubernetes, external
        Field('docker_compose_config', 'json'),
        Field('gpu_config', 'json'),
        Field('resource_limits', 'json'),
        Field('status', 'string', default='unknown'),  # running, stopped, pulling, error
        Field('health_status', 'string'),
        Field('last_health_check', 'datetime'),
        Field('auto_start', 'boolean', default=True),
        Field('created_at', 'datetime', default=datetime.utcnow)
    )

    # Ollama Models
    db.define_table('ollama_models',
        Field('deployment_id', 'reference ollama_deployments', required=True),
        Field('model_name', required=True),
        Field('model_tag', default='latest'),
        Field('status', 'string', default='unknown'),  # available, pulling, failed, removed
        Field('size_bytes', 'bigint'),
        Field('pull_progress', 'json'),
        Field('last_updated', 'datetime', default=datetime.utcnow),
        Field('auto_pull', 'boolean', default=False)
    )

    # Virtual Keys (WaddleAI keys mapped to AILB)
    db.define_table('virtual_keys',
        Field('user_id', 'reference users'),
        Field('organization_id', 'reference organizations'),
        Field('name', required=True),
        Field('key_prefix', 'string'),
        Field('key_hash', 'password'),
        Field('ailb_key_id', 'string'),
        Field('ailb_sync_status', 'string', default='pending'),
        Field('allowed_models', 'json'),
        Field('allowed_providers', 'json'),
        Field('budget_limit_daily', 'double'),
        Field('budget_limit_monthly', 'double'),
        Field('tpm_limit', 'integer', default=10000),
        Field('rpm_limit', 'integer', default=60),
        Field('enabled', 'boolean', default=True),
        Field('expires_at', 'datetime'),
        Field('last_used', 'datetime'),
        Field('created_at', 'datetime', default=datetime.utcnow)
    )

    # AILB Usage Events (webhooks)
    db.define_table('ailb_usage_events',
        Field('event_id', 'string', unique=True),
        Field('virtual_key_id', 'reference virtual_keys'),
        Field('ailb_key_id', 'string'),
        Field('request_id', 'string'),
        Field('model', 'string'),
        Field('provider', 'string'),
        Field('input_tokens', 'integer'),
        Field('output_tokens', 'integer'),
        Field('cost_usd', 'double'),
        Field('latency_ms', 'integer'),
        Field('status', 'string'),
        Field('error_message', 'text'),
        Field('timestamp', 'datetime'),
        Field('processed', 'boolean', default=False),
        Field('created_at', 'datetime', default=datetime.utcnow)
    )

    # Token Conversion Rates
    db.define_table('token_conversion_rates',
        Field('provider', 'string', required=True),
        Field('model', 'string', required=True),
        Field('input_rate', 'double', required=True),
        Field('output_rate', 'double', required=True),
        Field('base_cost_per_waddleai_token', 'double', default=0.001),
        Field('effective_date', 'datetime', default=datetime.utcnow),
        Field('enabled', 'boolean', default=True)
    )

    # Token Usage Tracking
    db.define_table('token_usage',
        Field('virtual_key_id', 'reference virtual_keys'),
        Field('user_id', 'reference users'),
        Field('organization_id', 'reference organizations'),
        Field('date', 'date'),
        Field('waddleai_tokens', 'integer', default=0),
        Field('llm_tokens', 'json'),
        Field('tokens_input_total', 'integer', default=0),
        Field('tokens_output_total', 'integer', default=0),
        Field('request_count', 'integer', default=0),
        Field('cost_usd_total', 'double', default=0.0),
        Field('last_updated', 'datetime', default=datetime.utcnow)
    )

    # Usage Cache (real-time quota enforcement)
    db.define_table('usage_cache',
        Field('virtual_key_id', 'reference virtual_keys'),
        Field('organization_id', 'reference organizations'),
        Field('period', 'string', required=True),
        Field('period_start', 'datetime', required=True),
        Field('waddleai_tokens_used', 'integer', default=0),
        Field('llm_tokens_used', 'json'),
        Field('requests_made', 'integer', default=0),
        Field('last_updated', 'datetime', default=datetime.utcnow)
    )

    # Security Logs
    db.define_table('security_logs',
        Field('timestamp', 'datetime', default=datetime.utcnow),
        Field('virtual_key_id', 'reference virtual_keys'),
        Field('user_id', 'reference users'),
        Field('organization_id', 'reference organizations'),
        Field('request_hash', 'string'),
        Field('threat_type', 'string'),
        Field('severity', 'string'),
        Field('blocked', 'boolean', default=False),
        Field('prompt_sample', 'text'),
        Field('detection_rules', 'json'),
        Field('ip_address', 'string')
    )

    # Usage Logs
    db.define_table('usage_logs',
        Field('timestamp', 'datetime', default=datetime.utcnow),
        Field('virtual_key_id', 'reference virtual_keys'),
        Field('user_id', 'reference users'),
        Field('organization_id', 'reference organizations'),
        Field('request_hash', 'string'),
        Field('provider_id', 'reference ai_providers'),
        Field('waddleai_tokens_used', 'integer', default=0),
        Field('llm_tokens_input', 'integer', default=0),
        Field('llm_tokens_output', 'integer', default=0),
        Field('llm_tokens_total', 'integer', default=0),
        Field('response_time', 'double'),
        Field('status_code', 'integer'),
        Field('model_used', 'string'),
        Field('provider_type', 'string'),
        Field('cost_estimate_waddleai', 'double'),
        Field('cost_estimate_usd', 'double')
    )

    # Routing Rules
    db.define_table('routing_rules',
        Field('name', required=True),
        Field('routing_llm_id', 'reference ai_providers'),
        Field('conditions', 'json'),
        Field('target_providers', 'list:reference ai_providers'),
        Field('priority', 'integer', default=100),
        Field('enabled', 'boolean', default=True)
    )

    # Legacy api_keys table (for migration)
    db.define_table('api_keys',
        Field('key_id', unique=True, required=True),
        Field('key_hash', 'password', required=True),
        Field('user_id', 'reference users', required=True),
        Field('organization_id', 'reference organizations', required=True),
        Field('name', 'string', required=True),
        Field('token_quota_monthly', 'integer'),
        Field('token_quota_daily', 'integer'),
        Field('rate_limit_rpm', 'integer', default=60),
        Field('default_model', 'string'),
        Field('enabled', 'boolean', default=True),
        Field('expires_at', 'datetime'),
        Field('last_used', 'datetime'),
        Field('created_at', 'datetime', default=datetime.utcnow),
        Field('permissions', 'json'),
        Field('allowed_endpoints', 'list:string'),
        Field('api_access_level', 'string'),
        Field('migrated_to_virtual_key', 'reference virtual_keys'),
        format='%(name)s'
    )


def init_redis(app: Flask) -> Optional[redis.Redis]:
    """Initialize Redis connection"""
    global redis_client

    redis_url = app.config.get('REDIS_URL')
    if not redis_url:
        logger.warning("Redis URL not configured, running without Redis")
        return None

    try:
        redis_client = redis.from_url(redis_url, decode_responses=True)
        redis_client.ping()
        logger.info("Redis connection established")
        return redis_client
    except Exception as e:
        logger.warning(f"Failed to connect to Redis: {e}")
        return None


def init_security(app: Flask, db: DAL) -> Security:
    """Initialize Flask-Security-Too"""
    global security

    user_datastore = PyDALUserDatastore(db)
    security = Security(app, user_datastore)

    logger.info("Flask-Security-Too initialized")
    return security


def init_extensions(app: Flask):
    """Initialize all Flask extensions"""
    global db, security, redis_client

    # Initialize database
    db = init_db(app)

    # Initialize Redis
    redis_client = init_redis(app)

    # Initialize Flask-Security-Too
    security = init_security(app, db)

    # Initialize default data
    init_default_data(db)


def init_default_data(db: DAL):
    """Initialize default data for the database"""
    from passlib.hash import bcrypt
    import secrets

    # Create default organization
    if not db(db.organizations.name == 'default').select():
        org_id = db.organizations.insert(
            name='default',
            description='Default organization for initial setup',
            token_quota_monthly=1000000,
            token_quota_daily=100000
        )
        logger.info("Created default organization")
    else:
        org_id = db(db.organizations.name == 'default').select().first().id

    # Create admin user if doesn't exist
    if not db(db.users.username == 'admin').select():
        admin_id = db.users.insert(
            username='admin',
            email='admin@waddleai.local',
            password_hash=bcrypt.hash('admin123'),
            role='admin',
            organization_id=org_id,
            token_quota_monthly=999999999,
            token_quota_daily=999999
        )

        # Create admin virtual key
        api_key = 'wa-' + secrets.token_urlsafe(32)
        db.virtual_keys.insert(
            user_id=admin_id,
            organization_id=org_id,
            name='Admin Master Key',
            key_prefix='wa-admin',
            key_hash=bcrypt.hash(api_key),
            tpm_limit=1000000,
            rpm_limit=10000,
            enabled=True
        )

        logger.info(f"Created admin user. API Key: {api_key}")
        print(f"Admin API Key (save this!): {api_key}")

    # Default token conversion rates
    default_rates = [
        ('openai', 'gpt-4o', 10, 20, 0.0025),
        ('openai', 'gpt-4o-mini', 30, 40, 0.0001),
        ('openai', 'gpt-4-turbo', 10, 20, 0.001),
        ('openai', 'gpt-4', 10, 20, 0.003),
        ('openai', 'gpt-3.5-turbo', 20, 30, 0.0005),
        ('anthropic', 'claude-3-5-sonnet-latest', 12, 18, 0.003),
        ('anthropic', 'claude-3-opus-20240229', 8, 15, 0.015),
        ('anthropic', 'claude-3-sonnet-20240229', 12, 18, 0.003),
        ('anthropic', 'claude-3-haiku-20240307', 25, 35, 0.00025),
        ('ollama', 'llama3.2', 50, 50, 0.0),
        ('ollama', 'llama3.1', 50, 50, 0.0),
        ('ollama', 'mistral', 45, 45, 0.0),
        ('gemini', 'gemini-1.5-pro', 12, 18, 0.00125),
        ('gemini', 'gemini-1.5-flash', 30, 40, 0.000075),
        ('cohere', 'command-r-plus', 15, 20, 0.003),
        ('cohere', 'command-r', 25, 30, 0.0005),
    ]

    for provider, model, input_rate, output_rate, cost in default_rates:
        if not db((db.token_conversion_rates.provider == provider) &
                  (db.token_conversion_rates.model == model)).select():
            db.token_conversion_rates.insert(
                provider=provider,
                model=model,
                input_rate=input_rate,
                output_rate=output_rate,
                base_cost_per_waddleai_token=cost
            )

    db.commit()
    logger.info("Default data initialized")
