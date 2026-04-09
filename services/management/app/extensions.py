"""
WaddleAI Management Server Extensions
Flask-Security-Too, penguin-dal, Redis initialization
"""

import logging
from datetime import datetime
from typing import Optional

import redis
from flask import Flask
from flask_security import RoleMixin, Security, UserMixin
from flask_security.datastore import UserDatastore
from penguin_dal.db import DB
from penguin_dal.flask_ext import init_dal

logger = logging.getLogger(__name__)

# Global instances
db: Optional[DB] = None
security: Optional[Security] = None
redis_client: Optional[redis.Redis] = None


class PyDALUserDatastore(UserDatastore):
    """Custom UserDatastore for PyDAL integration with Flask-Security-Too"""

    def __init__(self, db: DB):
        self.db = db

    def find_user(self, **kwargs):
        """Find user by any field"""
        query = None
        for key, value in kwargs.items():
            if key == "id":
                key = "id"
            condition = self.db.users[key] == value
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

        password = kwargs.pop("password", None)
        if password:
            kwargs["password_hash"] = bcrypt.hash(password)

        kwargs["created_at"] = datetime.utcnow()
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
        self.db(self.db.users.id == user.id).update(role="user")
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

    def __init__(self, row, db: DB):
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


def init_db(app: Flask) -> DB:
    """Initialize database with SQLAlchemy for schema, penguin-dal for runtime operations"""
    import time

    from app.models_sqlalchemy import init_schema

    global db

    # Wait for DNS to be ready (common issue in Kubernetes with --preload)
    logger.info("Waiting for DNS to initialize...")
    time.sleep(5)

    db_url = app.config["DATABASE_URL"]
    logger.info(f"Connecting to database: {db_url.split('@')[-1] if '@' in db_url else db_url}")

    max_retries = 10
    retry_delay = 2  # seconds

    for attempt in range(1, max_retries + 1):
        try:
            # Step 1: Initialize schema with SQLAlchemy (creates tables if needed)
            logger.info("Initializing database schema with SQLAlchemy...")
            init_schema(db_url)
            logger.info("Database schema initialized")

            # Step 2: Connect with penguin-dal for runtime operations (auto-reflects schema)
            db = init_dal(app)

            logger.info(f"Database initialized successfully on attempt {attempt}")
            return db

        except Exception as e:
            if attempt < max_retries:
                logger.warning(f"Database connection attempt {attempt}/{max_retries} failed: {e}")
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error(f"Failed to connect to database after {max_retries} attempts")
                raise


def init_redis(app: Flask) -> Optional[redis.Redis]:
    """Initialize Redis connection"""
    global redis_client

    redis_url = app.config.get("REDIS_URL")
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


def init_security(app: Flask, db: DB) -> Security:
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


def init_default_data(db: DB):
    """Initialize default data for the database"""
    import secrets

    from passlib.hash import bcrypt

    # Create default organization
    if not db(db.organizations.name == "default").select():
        org_id = db.organizations.insert(
            name="default",
            description="Default organization for initial setup",
            token_quota_monthly=1000000,
            token_quota_daily=100000,
        )
        logger.info("Created default organization")
    else:
        org_id = db(db.organizations.name == "default").select().first().id

    # Create admin user if doesn't exist
    if not db(db.users.username == "admin").select():
        admin_id = db.users.insert(
            username="admin",
            email="admin@localhost.local",
            password_hash=bcrypt.hash("admin123"),
            role="admin",
            organization_id=org_id,
            token_quota_monthly=999999999,
            token_quota_daily=999999,
        )

        # Create admin virtual key
        api_key = "wa-" + secrets.token_urlsafe(32)
        db.virtual_keys.insert(
            user_id=admin_id,
            organization_id=org_id,
            name="Admin Master Key",
            key_prefix="wa-admin",
            key_hash=bcrypt.hash(api_key),
            tpm_limit=1000000,
            rpm_limit=10000,
            enabled=True,
        )

        logger.info(f"Created admin user. API Key: {api_key}")
        print(f"Admin API Key (save this!): {api_key}")

    # Default token conversion rates
    default_rates = [
        ("openai", "gpt-4o", 10, 20, 0.0025),
        ("openai", "gpt-4o-mini", 30, 40, 0.0001),
        ("openai", "gpt-4-turbo", 10, 20, 0.001),
        ("openai", "gpt-4", 10, 20, 0.003),
        ("openai", "gpt-3.5-turbo", 20, 30, 0.0005),
        ("anthropic", "claude-3-5-sonnet-latest", 12, 18, 0.003),
        ("anthropic", "claude-3-opus-20240229", 8, 15, 0.015),
        ("anthropic", "claude-3-sonnet-20240229", 12, 18, 0.003),
        ("anthropic", "claude-3-haiku-20240307", 25, 35, 0.00025),
        ("ollama", "llama3.2", 50, 50, 0.0),
        ("ollama", "llama3.1", 50, 50, 0.0),
        ("ollama", "mistral", 45, 45, 0.0),
        ("gemini", "gemini-1.5-pro", 12, 18, 0.00125),
        ("gemini", "gemini-1.5-flash", 30, 40, 0.000075),
        ("cohere", "command-r-plus", 15, 20, 0.003),
        ("cohere", "command-r", 25, 30, 0.0005),
    ]

    for provider, model, input_rate, output_rate, cost in default_rates:
        if not db(
            (db.token_conversion_rates.provider == provider) & (db.token_conversion_rates.model == model)
        ).select():
            db.token_conversion_rates.insert(
                provider=provider,
                model=model,
                input_rate=input_rate,
                output_rate=output_rate,
                base_cost_per_waddleai_token=cost,
            )

    db.commit()
    logger.info("Default data initialized")
