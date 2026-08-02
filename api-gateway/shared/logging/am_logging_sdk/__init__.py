"""
AM Logging SDK Integration Package
Provides fire-and-forget logging capabilities for AM services
"""

from .am_logging_client import AMLoggingClient, LoggerMixin

__version__ = "1.0.0"
__all__ = ["AMLoggingClient", "LoggerMixin"]
