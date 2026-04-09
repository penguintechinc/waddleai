"""
WaddleAI Management Server Configuration
"""

import os
from datetime import timedelta


class Config:
    """Base configuration"""

    # Flask settings
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", os.getenv("JWT_SECRET", "change-in-production"))
    DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"

    # Database settings (PyDAL)
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite://waddleai.db")

    # Redis settings
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # JWT settings
    JWT_SECRET_KEY = os.getenv("JWT_SECRET", "change-in-production-min-32-chars")
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=24)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=30)

    # Flask-Security-Too settings
    SECURITY_PASSWORD_SALT = os.getenv("SECURITY_PASSWORD_SALT", "change-in-production")
    SECURITY_PASSWORD_HASH = "bcrypt"
    SECURITY_TOKEN_AUTHENTICATION_HEADER = "Authorization"
    SECURITY_TOKEN_AUTHENTICATION_KEY = "auth_token"
    SECURITY_TRACKABLE = True
    SECURITY_SEND_REGISTER_EMAIL = False
    SECURITY_REGISTERABLE = False
    SECURITY_RECOVERABLE = False
    SECURITY_CHANGEABLE = True

    # Session settings
    SESSION_TYPE = "redis"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=24)

    # MarchProxy AILB Integration
    MARCHPROXY_AILB_HOST = os.getenv("MARCHPROXY_AILB_HOST", "localhost")
    MARCHPROXY_AILB_GRPC_PORT = int(os.getenv("MARCHPROXY_AILB_GRPC_PORT", "50051"))
    MARCHPROXY_AILB_HTTP_PORT = int(os.getenv("MARCHPROXY_AILB_HTTP_PORT", "8080"))
    MARCHPROXY_AILB_TLS_ENABLED = os.getenv("MARCHPROXY_AILB_TLS_ENABLED", "false").lower() == "true"
    MARCHPROXY_AILB_TLS_CERT_PATH = os.getenv("MARCHPROXY_AILB_TLS_CERT_PATH", "")
    MARCHPROXY_AILB_TLS_KEY_PATH = os.getenv("MARCHPROXY_AILB_TLS_KEY_PATH", "")

    # Webhook settings
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-in-production")
    WEBHOOK_CALLBACK_URL = os.getenv("WEBHOOK_CALLBACK_URL", "http://localhost:8001/api/v1/webhooks/ailb/usage")

    # Ollama Management
    OLLAMA_MANAGEMENT_MODE = os.getenv("OLLAMA_MANAGEMENT_MODE", "both")  # manual, orchestrated, both
    DOCKER_HOST = os.getenv("DOCKER_HOST", "unix:///var/run/docker.sock")

    # Feature flags
    ENABLE_OLLAMA_MANAGEMENT = os.getenv("ENABLE_OLLAMA_MANAGEMENT", "true").lower() == "true"
    ENABLE_USAGE_WEBHOOKS = os.getenv("ENABLE_USAGE_WEBHOOKS", "true").lower() == "true"

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
    """Development configuration"""

    DEBUG = True
    LOG_LEVEL = "DEBUG"


class ProductionConfig(Config):
    """Production configuration"""

    DEBUG = False
    LOG_LEVEL = "INFO"

    # Force secure settings in production
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"


class TestingConfig(Config):
    """Testing configuration"""

    TESTING = True
    DEBUG = True
    DATABASE_URL = "sqlite://test_waddleai.db"
    REDIS_URL = "redis://localhost:6379/1"
