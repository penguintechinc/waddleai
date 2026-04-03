"""Logging for PenguinCode.

Uses Python's standard logging library.
- INFO/WARNING/ERROR always go to /tmp/penguincode-{epoch}.log
- DEBUG messages only appear when --debug flag is used
- Each session creates a new log file with epoch timestamp

Usage:
    from penguincode_cli.core import debug as log

    log.debug("Low-level detail - only with --debug")
    log.info("Normal operation info - always logged")
    log.warning("Something unexpected - always logged")
    log.error("Error occurred - always logged")
"""

import logging
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Generate log file with epoch timestamp (seconds since epoch)
_epoch_timestamp = int(time.time())
LOG_FILE = Path(tempfile.gettempdir()) / f"penguincode-{_epoch_timestamp}.log"

# Get the logger instance
_logger = logging.getLogger("penguincode")

# Track if debug mode is enabled (verbose logging)
_debug_enabled = False

# Flag to track if we've initialized logging
_initialized = False


def _init_logging() -> None:
    """Initialize basic logging (INFO level) to the log file."""
    global _initialized
    if _initialized:
        return

    _initialized = True

    # Set logger to INFO level by default
    _logger.setLevel(logging.INFO)

    # File handler - append mode
    file_handler = logging.FileHandler(LOG_FILE, mode="a")
    file_handler.setLevel(logging.DEBUG)  # Handler accepts all, logger filters

    # Format with timestamp and level
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(formatter)
    _logger.addHandler(file_handler)

    # Prevent propagation to root logger (avoids duplicate logs)
    _logger.propagate = False


def enable_debug() -> None:
    """Enable debug-level logging (more verbose output)."""
    global _debug_enabled

    # Ensure basic logging is initialized
    _init_logging()

    _debug_enabled = True

    # Upgrade logger to DEBUG level
    _logger.setLevel(logging.DEBUG)

    # Log startup
    _logger.info("=" * 60)
    _logger.info(f"PenguinCode Debug Session Started at {datetime.now()}")
    _logger.info(f"PID: {os.getpid()}")
    _logger.info("=" * 60)


# Initialize logging on module import (for INFO/WARNING/ERROR)
_init_logging()


def is_debug_enabled() -> bool:
    """Check if debug mode is enabled."""
    return _debug_enabled


def get_log_file() -> Path:
    """Get the current session's log file path."""
    return LOG_FILE


def debug(message: str, *args: Any, **kwargs: Any) -> None:
    """Log a debug message (only appears when --debug flag is used)."""
    _logger.debug(message, *args, **kwargs)


def info(message: str, *args: Any, **kwargs: Any) -> None:
    """Log an info message (always logged to file)."""
    _logger.info(message, *args, **kwargs)


def warning(message: str, *args: Any, **kwargs: Any) -> None:
    """Log a warning message (always logged to file)."""
    _logger.warning(message, *args, **kwargs)


def error(message: str, *args: Any, **kwargs: Any) -> None:
    """Log an error message (always logged to file)."""
    _logger.error(message, *args, **kwargs)


def exception(message: str, *args: Any, **kwargs: Any) -> None:
    """Log an exception with traceback (always logged to file)."""
    _logger.exception(message, *args, **kwargs)


def log_llm_request(model: str, messages: list, tools: list = None) -> None:
    """Log an LLM request (INFO level, details at DEBUG)."""
    _logger.info(f"LLM REQUEST to model={model}")

    # Verbose details only at DEBUG level
    if _debug_enabled:
        _logger.debug(f"  Messages ({len(messages)}):")
        for i, msg in enumerate(messages):
            role = getattr(msg, 'role', 'unknown')
            content = getattr(msg, 'content', str(msg))
            # Truncate long content
            if len(content) > 500:
                content = content[:500] + "..."
            _logger.debug(f"    [{i}] {role}: {content}")

        if tools:
            _logger.debug(f"  Tools ({len(tools)}): {[t.get('function', {}).get('name', 'unknown') for t in tools]}")


def log_llm_response(response_text: str, tool_calls: list = None) -> None:
    """Log an LLM response (INFO level, details at DEBUG)."""
    _logger.info(f"LLM RESPONSE ({len(response_text)} chars)")

    # Verbose details only at DEBUG level
    if _debug_enabled:
        if len(response_text) > 1000:
            _logger.debug(f"  Text: {response_text[:1000]}...")
        else:
            _logger.debug(f"  Text: {response_text}")

    if tool_calls:
        _logger.info(f"  Tool calls ({len(tool_calls)}):")
        if _debug_enabled:
            for tc in tool_calls:
                name = tc.get("name") or tc.get("function", {}).get("name", "unknown")
                args = tc.get("arguments") or tc.get("function", {}).get("arguments", {})
                _logger.debug(f"    - {name}: {args}")


def log_tool_execution(tool_name: str, args: dict, result: Any) -> None:
    """Log a tool execution (INFO level, details at DEBUG)."""
    _logger.info(f"TOOL EXECUTE: {tool_name}")

    # Verbose details only at DEBUG level
    if _debug_enabled:
        _logger.debug(f"  Args: {args}")
        result_str = str(result)
        if len(result_str) > 500:
            result_str = result_str[:500] + "..."
        _logger.debug(f"  Result: {result_str}")


def log_agent_spawn(agent_type: str, task: str, complexity: str = None) -> None:
    """Log an agent spawn (INFO level, task at DEBUG)."""
    extra = f" (complexity={complexity})" if complexity else ""
    _logger.info(f"AGENT SPAWN: {agent_type}{extra}")

    if _debug_enabled:
        _logger.debug(f"  Task: {task}")


def log_agent_result(agent_type: str, success: bool, output: str) -> None:
    """Log an agent result (INFO level, output at DEBUG)."""
    status = "SUCCESS" if success else "FAILED"
    _logger.info(f"AGENT RESULT: {agent_type} - {status}")

    # Verbose output only at DEBUG level
    if _debug_enabled:
        if len(output) > 500:
            _logger.debug(f"  Output: {output[:500]}...")
        else:
            _logger.debug(f"  Output: {output}")


def log_error(context: str, exc: Exception) -> None:
    """Log an error with context (always logged)."""
    _logger.error(f"ERROR in {context}: {type(exc).__name__}: {exc}")


def log_intent_detection(user_message: str, detected_intent: str) -> None:
    """Log user intent detection (INFO level, message at DEBUG)."""
    _logger.info(f"INTENT DETECTED: {detected_intent}")

    if _debug_enabled:
        msg_preview = user_message[:200] + "..." if len(user_message) > 200 else user_message
        _logger.debug(f"  User message: {msg_preview}")
