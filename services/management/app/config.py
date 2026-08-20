"""WaddleAI Management Server Configuration."""

import os
from datetime import timedelta


def _build_database_url() -> str:
    """Build DATABASE_URL from environment variables."""
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url

    # If DATABASE_URL not set, construct from DB_TYPE and individual variables
    db_type = os.getenv("DB_TYPE", "sqlite").lower()

    if db_type == "postgresql":
        db_user = os.getenv("POSTGRES_USER", os.getenv("DB_USER", ""))
        db_pass = os.getenv("POSTGRES_PASSWORD", os.getenv("DB_PASS", ""))
        db_host = os.getenv("POSTGRES_HOST", "localhost")
        db_port = os.getenv("POSTGRES_PORT", "5432")
        db_name = os.getenv("POSTGRES_DB", "waddleai")

        if db_user and db_pass:
            return f"postgresql://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"
        else:
            return f"postgresql://{db_host}:{db_port}/{db_name}"

    elif db_type == "mysql":
        db_user = os.getenv("MYSQL_USER", os.getenv("DB_USER", ""))
        db_pass = os.getenv("MYSQL_PASSWORD", os.getenv("DB_PASS", ""))
        db_host = os.getenv("MYSQL_HOST", "localhost")
        db_port = os.getenv("MYSQL_PORT", "3306")
        db_name = os.getenv("MYSQL_DATABASE", os.getenv("DB_NAME", "waddleai"))

        if db_user and db_pass:
            return f"mysql+pymysql://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"
        else:
            return f"mysql+pymysql://{db_host}:{db_port}/{db_name}"

    else:  # sqlite
        db_path = os.getenv("SQLITE_PATH", "waddleai.db")
        # SQLite requires 3 slashes for relative paths
        if db_path.startswith("/"):
            return f"sqlite:///{db_path}"
        else:
            return f"sqlite:///{db_path}"


class Config:
    """Base configuration."""

    # Flask settings
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", os.getenv("JWT_SECRET", ""))
    DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"

    # Database settings (PyDAL)
    DATABASE_URL = _build_database_url()

    # Cache settings (house-standard CACHE_* env)
    CACHE_HOST = os.getenv("CACHE_HOST", "")
    CACHE_PORT = int(os.getenv("CACHE_PORT", "6379"))
    CACHE_USER = os.getenv("CACHE_USER", "")
    CACHE_PASS = os.getenv("CACHE_PASS", "")

    # Redis settings (deprecated alias for CACHE_* — honored for one release)
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # JWT settings
    JWT_SECRET_KEY = os.getenv("JWT_SECRET", "")
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=24)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=30)

    # Flask-Security-Too settings
    SECURITY_PASSWORD_SALT = os.getenv("SECURITY_PASSWORD_SALT", "")
    SECURITY_PASSWORD_HASH = "bcrypt"  # nosec B105 # noqa: S105 -- hashing ALGORITHM name, not a password
    SECURITY_TOKEN_AUTHENTICATION_HEADER = "Authorization"  # nosec B105 # noqa: S105 -- HTTP header name, not a credential
    SECURITY_TOKEN_AUTHENTICATION_KEY = "auth_token"  # nosec B105 # noqa: S105 -- session key NAME, not a token value
    SECURITY_TRACKABLE = True
    SECURITY_SEND_REGISTER_EMAIL = False
    SECURITY_REGISTERABLE = False
    SECURITY_RECOVERABLE = False
    SECURITY_CHANGEABLE = True

    # Session settings
    SESSION_TYPE = "redis"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=24)

    # Webhook settings
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

    # Admin initial password (sourced from env; handled per-config-class)
    ADMIN_INITIAL_PASSWORD = os.getenv("ADMIN_INITIAL_PASSWORD", "")

    # Ollama Management
    OLLAMA_MANAGEMENT_MODE = os.getenv(
        "OLLAMA_MANAGEMENT_MODE", "both"
    )  # manual, orchestrated, both
    DOCKER_HOST = os.getenv("DOCKER_HOST", "unix:///var/run/docker.sock")

    # Feature flags
    ENABLE_OLLAMA_MANAGEMENT = os.getenv("ENABLE_OLLAMA_MANAGEMENT", "true").lower() == "true"

    # Enterprise provider flags
    ENABLE_GEMINI = os.getenv("ENABLE_GEMINI", "true").lower() == "true"
    ENABLE_BEDROCK = os.getenv("ENABLE_BEDROCK", "true").lower() == "true"
    ENABLE_AZURE_OPENAI = os.getenv("ENABLE_AZURE_OPENAI", "true").lower() == "true"
    ENABLE_COHERE = os.getenv("ENABLE_COHERE", "true").lower() == "true"

    # CORS settings
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")

    # Logging
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


class DevelopmentConfig(Config):
    """Development configuration."""

    import secrets

    DEBUG = True
    LOG_LEVEL = "DEBUG"

    # Development: use deterministic defaults if env vars not set
    SECRET_KEY = os.getenv(
        "FLASK_SECRET_KEY", os.getenv("JWT_SECRET", "dev-secret-key-min-32-chars")
    )
    JWT_SECRET_KEY = os.getenv("JWT_SECRET", "dev-jwt-secret-key-32-chars-minimum!")
    SECURITY_PASSWORD_SALT = os.getenv("SECURITY_PASSWORD_SALT", "dev-password-salt")
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "dev-webhook-secret")
    ADMIN_INITIAL_PASSWORD = os.getenv("ADMIN_INITIAL_PASSWORD", "dev-admin-password")


class ProductionConfig(Config):
    """Production configuration."""

    DEBUG = False
    LOG_LEVEL = "INFO"

    # Force secure settings in production
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # Production: require all secrets from environment
    # These will be empty strings if not set; validation happens in init_default_data
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", os.getenv("JWT_SECRET", ""))
    JWT_SECRET_KEY = os.getenv("JWT_SECRET", "")
    SECURITY_PASSWORD_SALT = os.getenv("SECURITY_PASSWORD_SALT", "")
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
    ADMIN_INITIAL_PASSWORD = os.getenv("ADMIN_INITIAL_PASSWORD", "")


class TestingConfig(Config):
    """Testing configuration."""

    import secrets

    TESTING = True
    DEBUG = True
    # Bug fix: literal was "sqlite://test_waddleai.db" (missing slash -> invalid
    # SQLAlchemy URL, raises ArgumentError). Also now honors DATABASE_URL env var
    # (e.g. set by the contract-snapshot harness) like the base Config does.
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///test_waddleai.db")
    REDIS_URL = "redis://localhost:6379/1"

    # Testing: use deterministic defaults to bootstrap tests
    SECRET_KEY = os.getenv(
        "FLASK_SECRET_KEY", os.getenv("JWT_SECRET", "test-secret-key-min-32-chars-!!!!")
    )
    JWT_SECRET_KEY = os.getenv("JWT_SECRET", "test-jwt-secret-key-32-chars-minimum!!!")
    SECURITY_PASSWORD_SALT = os.getenv("SECURITY_PASSWORD_SALT", "test-password-salt")
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "test-webhook-secret")

    # Testing: generate deterministic admin password if env var not set
    _testing_admin_password = os.getenv("ADMIN_INITIAL_PASSWORD", None)
    if _testing_admin_password is None:
        # Generate a deterministic test password (same across runs for reproducibility)
        import hashlib

        _seed = hashlib.sha256(b"waddleai-test-admin").hexdigest()[:16]
        ADMIN_INITIAL_PASSWORD = f"test-admin-{_seed}"
    else:
        ADMIN_INITIAL_PASSWORD = _testing_admin_password
