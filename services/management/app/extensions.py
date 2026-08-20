"""WaddleAI Management Server Extensions.

penguin-dal, Redis initialization.

Auth is handled entirely via OIDC through shared.auth.penguin_auth (see
app/api/v1/auth.py) -- Flask-Security-Too is not used anywhere in this
service and its glue (PyDALUserDatastore/PyDALUser/PyDALRole/init_security)
has been removed.
"""

import logging
from datetime import datetime
from urllib.parse import quote

import redis
from penguin_dal.db import DB
from penguin_dal.flask_ext import init_dal
from quart import Quart

logger = logging.getLogger(__name__)

# Global instances
db: DB | None = None
redis_client: redis.Redis | None = None
cache_client: redis.Redis | None = None  # Alias for redis_client


def init_db(app: Quart) -> DB:
    """Initialize database with SQLAlchemy for schema, penguin-dal for runtime operations."""
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
            # penguin-dal 0.1.0 (released) init_dal returns None and parks the DB on
            # app.extensions["_penguin_dal"]; newer penguin-dal returns it directly.
            db = init_dal(app, uri=db_url, pool_size=int(app.config.get("DB_POOL_SIZE", 10)))
            if db is None:
                db = app.extensions["_penguin_dal"]

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


def init_cache(app: Quart) -> redis.Redis | None:
    """Initialize cache (Valkey/Redis) connection with CACHE_* env precedence."""
    global redis_client, cache_client

    # Precedence: CACHE_HOST > REDIS_URL
    cache_host = app.config.get("CACHE_HOST")
    if cache_host:
        # Build redis-protocol URL from CACHE_* components
        cache_port = app.config.get("CACHE_PORT", 6379)
        cache_user = app.config.get("CACHE_USER", "")
        cache_pass = app.config.get("CACHE_PASS", "")

        # Build URL with optional user:pass@ segment (URL-encode password)
        if cache_user and cache_pass:
            # URL-encode password to handle special characters
            encoded_pass = quote(cache_pass, safe="")
            cache_url = f"redis://{cache_user}:{encoded_pass}@{cache_host}:{cache_port}/0"
        elif cache_user:
            cache_url = f"redis://{cache_user}@{cache_host}:{cache_port}/0"
        else:
            cache_url = f"redis://{cache_host}:{cache_port}/0"
    else:
        # Fall back to REDIS_URL (deprecated)
        cache_url = app.config.get("REDIS_URL")
        if cache_url:
            logger.warning(
                "REDIS_URL is deprecated; set CACHE_HOST/CACHE_PORT (honored for one release)"
            )

    if not cache_url:
        logger.warning("Cache URL not configured, running without cache")
        return None

    try:
        redis_client = redis.from_url(cache_url, decode_responses=True)
        redis_client.ping()
        # Also set cache_client alias to same object
        cache_client = redis_client
        logger.info("Cache connection established")
        return redis_client
    except Exception as e:
        logger.warning(f"Failed to connect to cache: {e}")
        return None


# Backward-compat shim for external callers using old name
init_redis = init_cache


def init_extensions(app: Quart):
    """Initialize all extensions."""
    global db, redis_client, cache_client

    # Initialize database
    db = init_db(app)

    # Initialize cache
    redis_client = init_cache(app)
    cache_client = redis_client  # Ensure alias is set

    # Initialize default data (pass config so it can read ADMIN_INITIAL_PASSWORD)
    init_default_data(db, config=app.config)


def init_default_data(db: DB, config: dict | None = None) -> str | None:
    """Initialize default data for the database.

    Args:
        db: Database instance
        config: Flask app config object (used to read ADMIN_INITIAL_PASSWORD)

    Returns:
        The generated admin password (only for testing/development; production
        should never log this)

    """
    import secrets

    from passlib.hash import bcrypt

    # Use provided config or fall back to None (caller should provide it)
    admin_password = None
    if config:
        admin_password = config.get("ADMIN_INITIAL_PASSWORD", "")

    if not admin_password:
        # No ADMIN_INITIAL_PASSWORD provided: generate a random, un-loggable
        # password. The admin account is then unusable until reset via an
        # operator flow -- fail-closed (no known default credential).
        admin_password = secrets.token_urlsafe(16)

    # Tracks the initial admin password to return (only populated when a new
    # admin is created; consumed by dev/test callers, never logged).
    result_password: str | None = None

    # Create default organization
    if not db(db.organizations.name == "default").select():
        org_id = db.organizations.insert(
            name="default",
            description="Default organization for initial setup",
            token_quota_monthly=1000000,
            token_quota_daily=100000,
            enabled=True,
            created_at=datetime.utcnow(),
        )
        logger.info("Created default organization")
    else:
        org_id = db(db.organizations.name == "default").select().first().id

    # Create admin user if doesn't exist
    if not db(db.users.username == "admin").select():
        admin_id = db.users.insert(
            username="admin",
            email="admin@localhost.local",
            password_hash=bcrypt.hash(admin_password),
            role="admin",
            organization_id=org_id,
            token_quota_monthly=999999999,
            token_quota_daily=999999,
            enabled=True,
            created_at=datetime.utcnow(),
        )

        # Create admin virtual key
        api_key = "wa-" + secrets.token_urlsafe(32)
        db.virtual_keys.insert(
            user_id=admin_id,
            organization_id=org_id,  # INVARIANT: must match admin user's org_id
            name="Admin Master Key",
            key_prefix="wa-admin",
            key_hash=bcrypt.hash(api_key),
            tpm_limit=1000000,
            rpm_limit=10000,
            enabled=True,
        )

        logger.info("Created admin user and virtual key")
        # Never log or print the plaintext API key
        result_password = admin_password

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
            (db.token_conversion_rates.provider == provider)
            & (db.token_conversion_rates.model == model)
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
    return result_password
