"""
WaddleAI Management API v1
"""

from flask import Blueprint

api_v1_bp = Blueprint('api_v1', __name__)

# Import all route modules to register them
from . import auth
from . import users
from . import organizations
from . import providers
from . import ollama
from . import ollama_models
from . import ailb
from . import ailb_memory
from . import keys
from . import usage
from . import quotas
from . import webhooks
