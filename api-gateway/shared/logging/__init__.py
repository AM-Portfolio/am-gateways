"""
Centralized Advanced Logging Infrastructure for AM Portfolio Services

This package provides comprehensive logging capabilities including:
- Structured JSON logging with correlation IDs
- FastAPI middleware for automatic request tracking
- Performance monitoring and timing
- Configurable log levels per service
- File rotation and console output
- Context-aware logging with async support
- Production-ready logging with sampling

Quick Start:
    # Development setup
    from shared.logging import quick_setup_development, get_logger
    quick_setup_development("my-service")
    logger = get_logger("my-service")
    logger.info("Hello world!")

    # Production setup
    from shared.logging import quick_setup_production, get_logger
    quick_setup_production("my-service", "/var/log/my-service.log")
    
    # FastAPI integration
    from shared.logging import initialize_auth_tokens_logging
    from shared.logging.middleware import setup_fastapi_logging
    
    initialize_auth_tokens_logging()
    app = FastAPI()
    setup_fastapi_logging(app, service_name="am-auth-tokens")
"""

# Core logging functionality
from .logger import (
    LogConfig,
    AMLogger,
    setup_logging,
    get_logger,
    set_correlation_context,
    clear_correlation_context,
    generate_correlation_id,
    log_execution_time,
    log_async_execution_time,
    LoggerMixin
)

# Configuration management
from .config import (
    get_service_log_config,
    setup_auth_tokens_logging,
    setup_user_management_logging,
    config_manager
)

# Setup utilities
from .setup import (
    initialize_logging,
    initialize_auth_tokens_logging,
    initialize_user_management_logging,
    quick_setup_development,
    quick_setup_production,
    quick_setup_testing
)

# FastAPI middleware (optional import)
try:
    from .middleware import LoggingMiddleware, setup_fastapi_logging
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False
    LoggingMiddleware = None
    setup_fastapi_logging = None

__all__ = [
    # Core
    "LogConfig",
    "AMLogger", 
    "setup_logging",
    "get_logger",
    "set_correlation_context",
    "clear_correlation_context", 
    "generate_correlation_id",
    "log_execution_time",
    "log_async_execution_time",
    "LoggerMixin",
    
    # Configuration
    "get_service_log_config",
    "setup_auth_tokens_logging",
    "setup_user_management_logging",
    "config_manager",
    
    # Setup
    "initialize_logging",
    "initialize_auth_tokens_logging",
    "initialize_user_management_logging",
    "quick_setup_development",
    "quick_setup_production", 
    "quick_setup_testing",
    
    # FastAPI (if available)
    "LoggingMiddleware",
    "setup_fastapi_logging"
]

# Expose availability flag
FASTAPI_AVAILABLE = _FASTAPI_AVAILABLE