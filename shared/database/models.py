"""
WaddleAI Database Models
Shared database models for both proxy and management servers
"""

from pydal import DAL, Field
from datetime import datetime, date
import os


def get_db(db_uri=None):
    """Initialize database connection with all models"""
    if db_uri is None:
        db_uri = os.getenv('DATABASE_URL', 'sqlite://waddleai.db')
    
    # migrate=False: Alembic is the sole schema authority. Do not auto-migrate here.
    db = DAL(db_uri, migrate=False, fake_migrate_all=False)
    define_tables(db)
    return db


def define_tables(db):
    """Define all database tables"""
    
    # Organizations for Multi-tenancy
    db.define_table('organizations',
        Field('name', unique=True, required=True),
        Field('description', 'text'),
        Field('token_quota_monthly', 'integer', default=1000000),
        Field('token_quota_daily', 'integer', default=100000),
        Field('default_model', 'string'),  # Default model for organization
        Field('enabled', 'boolean', default=True),
        Field('created_at', 'datetime', default=datetime.utcnow),
        format='%(name)s'
    )

    # Users and Authentication
    db.define_table('users',
        Field('username', unique=True, required=True),
        Field('email', unique=True, required=True),
        Field('password_hash', 'password', required=True),
        Field('role', 'string', required=True),  # admin, resource_manager, reporter, user
        Field('organization_id', 'reference organizations', required=True),
        Field('managed_orgs', 'list:reference organizations'),  # For resource managers
        Field('created_at', 'datetime', default=datetime.utcnow),
        Field('token_quota_monthly', 'integer', default=100000),
        Field('token_quota_daily', 'integer', default=10000),
        Field('default_model', 'string'),  # Default model for user
        Field('enabled', 'boolean', default=True),
        format='%(username)s'
    )

    # API Keys with Usage Limits
    db.define_table('api_keys',
        Field('key_id', unique=True, required=True),
        Field('key_hash', 'password', required=True),  # Hashed API key
        Field('user_id', 'reference users', required=True),
        Field('organization_id', 'reference organizations', required=True),
        Field('name', 'string', required=True),  # Human readable name
        Field('token_quota_monthly', 'integer'),  # Override user quota
        Field('token_quota_daily', 'integer'),    # Override user quota
        Field('rate_limit_rpm', 'integer', default=60),  # Requests per minute
        Field('default_model', 'string'),  # Default model for this API key
        Field('enabled', 'boolean', default=True),
        Field('expires_at', 'datetime'),
        Field('last_used', 'datetime'),
        Field('created_at', 'datetime', default=datetime.utcnow),
        Field('permissions', 'json'),  # Scoped permissions
        Field('allowed_endpoints', 'list:string'),  # Endpoint restrictions
        Field('api_access_level', 'string'),  # admin_api, management_api, proxy_api
        format='%(name)s'
    )

    # Connection Links (LLM Providers)
    # TODO: This file uses raw PyDAL and is used by the proxy service.
    # The management service uses penguin-dal (auto-reflects schema from SQLAlchemy models).
    # provider_credentials is NOT defined here because the proxy service should be
    # migrated to penguin-dal in a dedicated ticket — at that point this file is removed.
    # See: services/management/app/models_sqlalchemy.py for the canonical schema.
    db.define_table('connection_links',
        Field('name', unique=True, required=True),
        Field('provider', 'string', required=True),  # ollama, anthropic, openai
        Field('endpoint_url', required=True),
        Field('api_key', 'password'),
        Field('model_list', 'json'),
        Field('rate_limits', 'json'),
        Field('enabled', 'boolean', default=True),
        Field('tls_config', 'json'),
        Field('management_capabilities', 'json'),  # For Ollama: pull, remove, list
        format='%(name)s'
    )

    # Ollama Model Registry
    db.define_table('ollama_models',
        Field('link_id', 'reference connection_links', required=True),
        Field('model_name', required=True),
        Field('model_tag', default='latest'),
        Field('status', 'string', default='unknown'),  # available, pulling, failed, removed
        Field('size_bytes', 'bigint'),
        Field('pull_progress', 'json'),
        Field('last_updated', 'datetime', default=datetime.utcnow),
        Field('auto_pull', 'boolean', default=False)
    )

    # Routing Rules
    db.define_table('routing_rules',
        Field('name', required=True),
        Field('routing_llm_id', 'reference connection_links'),
        Field('conditions', 'json'),  # request patterns, user criteria
        Field('target_links', 'list:reference connection_links'),
        Field('priority', 'integer', default=100),
        Field('enabled', 'boolean', default=True)
    )

    # Conversation Memory Configurations
    db.define_table('conversation_memory_configs',
        Field('name', required=True),
        Field('provider', 'string', default='mem0'),  # mem0, chromadb
        Field('connection_string'),
        Field('api_key', 'password'),
        Field('collection_name'),
        Field('embedding_model'),
        Field('config_json', 'json'),  # Provider-specific settings
        Field('enabled', 'boolean', default=True)
    )

    # RAG/Knowledge Base Configurations
    db.define_table('rag_configs',
        Field('name', required=True),
        Field('provider', 'string', default='supabase'),  # supabase, qdrant, chromadb
        Field('connection_string'),
        Field('api_key', 'password'),
        Field('collection_name'),
        Field('embedding_model', default='all-MiniLM-L6-v2'),
        Field('config_json', 'json'),  # Provider-specific settings (host, port, etc.)
        Field('chunk_size', 'integer', default=512),
        Field('chunk_overlap', 'integer', default=50),
        Field('enabled', 'boolean', default=True)
    )

    # Token Conversion Rates (LLM tokens to WaddleAI tokens)
    db.define_table('token_conversion_rates',
        Field('provider', 'string', required=True),  # openai, anthropic, ollama
        Field('model', 'string', required=True),
        Field('input_rate', 'double', required=True),   # LLM tokens per WaddleAI token
        Field('output_rate', 'double', required=True),  # LLM tokens per WaddleAI token
        Field('base_cost_per_waddleai_token', 'double', default=0.001),
        Field('effective_date', 'datetime', default=datetime.utcnow),
        Field('enabled', 'boolean', default=True)
    )

    # Token Usage Tracking
    db.define_table('token_usage',
        Field('api_key_id', 'reference api_keys', required=True),
        Field('user_id', 'reference users', required=True),
        Field('organization_id', 'reference organizations', required=True),
        Field('date', 'date', default=date.today),
        # WaddleAI Tokens (normalized usage units)
        Field('waddleai_tokens', 'integer', default=0),
        # Individual LLM Token Counts
        Field('llm_tokens', 'json'),  # {"openai_gpt4": {"input": 100, "output": 50}, "claude": {...}}
        Field('tokens_input_total', 'integer', default=0),  # Sum across all LLMs
        Field('tokens_output_total', 'integer', default=0), # Sum across all LLMs
        Field('request_count', 'integer', default=0),
        Field('last_updated', 'datetime', default=datetime.utcnow)
    )

    # Real-time Usage Cache (for quota enforcement)
    db.define_table('usage_cache',
        Field('api_key_id', 'reference api_keys', required=True),
        Field('organization_id', 'reference organizations', required=True),
        Field('period', 'string', required=True),  # daily, monthly
        Field('period_start', 'datetime', required=True),
        Field('waddleai_tokens_used', 'integer', default=0),
        Field('llm_tokens_used', 'json'),  # Per-LLM breakdown
        Field('requests_made', 'integer', default=0),
        Field('last_updated', 'datetime', default=datetime.utcnow)
    )

    # Prompt Security Logs
    db.define_table('security_logs',
        Field('timestamp', 'datetime', default=datetime.utcnow),
        Field('api_key_id', 'reference api_keys'),
        Field('user_id', 'reference users'),
        Field('organization_id', 'reference organizations'),
        Field('request_hash'),
        Field('threat_type', 'string'),  # injection, jailbreak, data_extraction
        Field('severity', 'string'),     # low, medium, high, critical
        Field('blocked', 'boolean', default=False),
        Field('prompt_sample', 'text'),  # Truncated sample for analysis
        Field('detection_rules', 'json'),
        Field('ip_address', 'string')
    )

    # Usage Analytics
    db.define_table('usage_logs',
        Field('timestamp', 'datetime', default=datetime.utcnow),
        Field('api_key_id', 'reference api_keys'),
        Field('user_id', 'reference users'),
        Field('organization_id', 'reference organizations'),
        Field('request_hash'),
        Field('routing_rule_id', 'reference routing_rules'),
        Field('target_link_id', 'reference connection_links'),
        # WaddleAI Token Usage
        Field('waddleai_tokens_used', 'integer', default=0),
        # Raw LLM Token Usage
        Field('llm_tokens_input', 'integer', default=0),
        Field('llm_tokens_output', 'integer', default=0),
        Field('llm_tokens_total', 'integer', default=0),
        Field('response_time', 'double'),
        Field('status_code', 'integer'),
        Field('model_used', 'string'),
        Field('provider', 'string'),
        Field('cost_estimate_waddleai', 'double'),  # Cost in WaddleAI tokens
        Field('cost_estimate_usd', 'double'),       # Estimated USD cost
        Field('security_check_passed', 'boolean', default=True)
    )

    return db


def init_default_data(db):
    """Initialize default data for the database"""
    
    # Create default organization
    if not db(db.organizations.name == 'default').select():
        org_id = db.organizations.insert(
            name='default',
            description='Default organization for initial setup',
            token_quota_monthly=1000000,
            token_quota_daily=100000
        )
    else:
        org_id = db(db.organizations.name == 'default').select().first().id

    # Create admin user if doesn't exist
    if not db(db.users.username == 'admin').select():
        from passlib.hash import bcrypt
        admin_id = db.users.insert(
            username='admin',
            email='admin@waddleai.local',
            password_hash=bcrypt.hash('admin123'),  # Change in production!
            role='admin',
            organization_id=org_id,
            token_quota_monthly=999999999,
            token_quota_daily=999999
        )

        # Create admin API key
        import secrets
        api_key = 'wa-' + secrets.token_urlsafe(32)
        api_key_hash = bcrypt.hash(api_key)
        
        db.api_keys.insert(
            key_id=f"admin-key-{secrets.token_hex(8)}",
            key_hash=api_key_hash,
            user_id=admin_id,
            organization_id=org_id,
            name='Admin Master Key',
            api_access_level='admin_api',
            permissions={'*': True}
        )
        
        print(f"Admin API Key (save this!): {api_key}")

    # Default token conversion rates
    default_rates = [
        ('openai', 'gpt-4', 10, 20),
        ('openai', 'gpt-3.5-turbo', 20, 30),
        ('anthropic', 'claude-3-opus', 8, 15),
        ('anthropic', 'claude-3-sonnet', 12, 18),
        ('ollama', 'llama2', 50, 50),
        ('ollama', 'mistral', 45, 45),
    ]
    
    for provider, model, input_rate, output_rate in default_rates:
        if not db((db.token_conversion_rates.provider == provider) & 
                 (db.token_conversion_rates.model == model)).select():
            db.token_conversion_rates.insert(
                provider=provider,
                model=model,
                input_rate=input_rate,
                output_rate=output_rate,
                base_cost_per_waddleai_token=0.001
            )

    db.commit()
    return db


if __name__ == '__main__':
    # Test database creation
    db = get_db()
    init_default_data(db)
    print("Database initialized successfully!")
    print(f"Tables: {', '.join(db.tables)}")