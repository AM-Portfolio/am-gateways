"""
Initialization and setup utilities for centralized logging
"""

import sys
import os
from pathlib import Path
from typing import Optional

# Add shared directory to Python path if needed
shared_dir = Path(__file__).parent.parent
if str(shared_dir) not in sys.path:
    sys.path.insert(0, str(shared_dir))

from .logger import setup_logging, get_logger, LogConfig, AMLogger
from .config import (
    get_service_log_config, 
    setup_auth_tokens_logging, 
    setup_user_management_logging,
    config_manager
)


def initialize_logging(service_name: str, config: Optional[LogConfig] = None) -> AMLogger:
    """
    Initialize logging for a specific service
    
    Args:
        service_name: Name of the service (e.g., "am-auth-tokens", "am-user-management")
        config: Optional LogConfig instance. If not provided, will be created from environment
    
    Returns:
        AMLogger: Configured logger instance
    """
    if config is None:
        config = get_service_log_config(service_name)
    
    # Setup logging
    logger_instance = setup_logging(config)
    
    # Initialize the auth adapter if possible
    try:
        from .auth_adapter import AMAuthLogger
        from . import auth_adapter as auth_adapter_mod
        auth_adapter_mod.auth_logger = AMAuthLogger(
            service_name=service_name,
            persist_to_db=config.persist_to_db
        )
    except Exception:
        pass
    
    # Log initialization message
    logger = get_logger(service_name)
    logger.info(
        f"Centralized logging initialized for {service_name}",
        extra={
            "service": service_name,
            "log_level": config.log_level,
            "log_format": config.log_format,
            "console_output": config.console_output,
            "file_output": config.file_output,
            "file_path": config.file_path,
            "environment": config.environment,
            "correlation_enabled": config.enable_correlation,
            "performance_enabled": config.enable_performance
        }
    )
    
    return logger_instance


def initialize_auth_tokens_logging() -> AMLogger:
    """Initialize logging specifically for auth-tokens service"""
    config = setup_auth_tokens_logging()
    return initialize_logging("am-auth-tokens", config)


def initialize_user_management_logging() -> AMLogger:
    """Initialize logging specifically for user-management service"""
    config = setup_user_management_logging()
    return initialize_logging("am-user-management", config)


# Convenience functions for FastAPI integration
def setup_fastapi_logging_for_auth_tokens():
    """Setup FastAPI logging middleware for auth-tokens service"""
    try:
        from .middleware import setup_fastapi_logging
        return lambda app: setup_fastapi_logging(
            app,
            service_name="am-auth-tokens",
            exclude_paths=["/health", "/metrics", "/docs", "/openapi.json", "/v1/health"]
        )
    except ImportError:
        # FastAPI not available - return no-op function
        return lambda app: None


def setup_fastapi_logging_for_user_management():
    """Setup FastAPI logging middleware for user-management service"""
    try:
        from .middleware import setup_fastapi_logging
        return lambda app: setup_fastapi_logging(
            app,
            service_name="am-user-management",
            exclude_paths=["/health", "/metrics", "/docs", "/openapi.json"]
        )
    except ImportError:
        # FastAPI not available - return no-op function
        return lambda app: None


# Quick setup functions for common scenarios
def quick_setup_development(service_name: str) -> AMLogger:
    """Quick setup for development environment"""
    config = LogConfig(
        service_name=service_name,
        log_level="DEBUG",
        log_format="structured",  # More readable for development
        console_output=True,
        file_output=False,
        enable_correlation=True,
        enable_performance=True,
        environment="development"
    )
    return initialize_logging(service_name, config)


def quick_setup_production(service_name: str, log_file_path: str) -> AMLogger:
    """Quick setup for production environment"""
    config = LogConfig(
        service_name=service_name,
        log_level="INFO",
        log_format="json",  # Structured JSON for log aggregation
        console_output=True,
        file_output=True,
        file_path=log_file_path,
        max_file_size=50 * 1024 * 1024,  # 50MB
        backup_count=10,
        enable_correlation=True,
        enable_performance=True,
        enable_sampling=False,  # Disable sampling in production
        environment="production"
    )
    return initialize_logging(service_name, config)


def quick_setup_testing(service_name: str) -> AMLogger:
    """Quick setup for testing environment"""
    config = LogConfig(
        service_name=service_name,
        log_level="WARNING",  # Less verbose for tests
        log_format="json",
        console_output=False,  # Reduce test output noise
        file_output=False,
        enable_correlation=False,  # Simpler for tests
        enable_performance=False,
        environment="testing"
    )
    return initialize_logging(service_name, config)


# Export commonly used items
__all__ = [
    "initialize_logging",
    "initialize_auth_tokens_logging", 
    "initialize_user_management_logging",
    "setup_fastapi_logging_for_auth_tokens",
    "setup_fastapi_logging_for_user_management",
    "quick_setup_development",
    "quick_setup_production",
    "quick_setup_testing",
    "get_logger",
    "LogConfig"
]
