"""
Advanced Centralized Logger for AM Portfolio Services

This module pro        # Create base log structure
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service_name,
            "environment": self.config.environment,
            "module": getattr(record, 'source_module', record.module if hasattr(record, 'module') else ''),
            "function": getattr(record, 'funcName', ''),
            "line": getattr(record, 'lineno', ''),
            "thread": getattr(record, 'thread', ''),
            "process": getattr(record, 'process', ''),
        }hensive logging solution with:
- Structured JSON logging
- Multiple output formats (JSON/text)
- Request correlation IDs
- Performance metrics
- Configurable log levels per service
- File rotation and console output
- Context-aware logging
- Async-safe logging
- Log sampling for high-traffic scenarios
"""

import logging
import json
import sys
import os
import uuid
import time
import functools
from typing import Any, Dict, Optional, Union, List
from datetime import datetime
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
import threading
from dataclasses import dataclass, asdict
import httpx
import asyncio

# Context variables for request tracking
correlation_id: ContextVar[Optional[str]] = ContextVar('correlation_id', default=None)
request_id: ContextVar[Optional[str]] = ContextVar('request_id', default=None)
user_id: ContextVar[Optional[str]] = ContextVar('user_id', default=None)
service_name: ContextVar[Optional[str]] = ContextVar('service_name', default=None)


@dataclass
class LogConfig:
    """Configuration class for logger settings"""
    service_name: str = "am-service"
    log_level: str = "INFO"
    log_format: str = "json"  # json, text, structured
    console_output: bool = True
    file_output: bool = False
    file_path: Optional[str] = None
    max_file_size: int = 10 * 1024 * 1024  # 10MB
    backup_count: int = 5
    rotation_type: str = "size"  # size, time
    time_rotation: str = "midnight"  # midnight, H, D
    enable_correlation: bool = True
    enable_performance: bool = True
    enable_sampling: bool = False
    sampling_rate: float = 1.0
    environment: str = "development"
    persist_to_db: bool = False
    cls_url: Optional[str] = None # URL for Centralized Logging Service
    structured_keys: List[str] = None

    def __post_init__(self):
        if self.structured_keys is None:
            self.structured_keys = [
                'timestamp', 'level', 'logger', 'message', 'service', 
                'correlation_id', 'request_id', 'user_id', 'duration_ms',
                'module', 'function', 'line'
            ]
        # Default CLS URL if not provided but can be overridden by env var in setup
        if not self.cls_url:
            self.cls_url = os.getenv("CLS_URL", "http://am-logging-svc/v1")


class CLSHandler(logging.Handler):
    """
    Custom logging handler to forward logs to AM Logging Service (CLS)
    """
    def __init__(self, service_name: str, cls_url: str, persist_to_db: bool = False):
        super().__init__()
        self.service_name = service_name
        self.cls_url = cls_url.rstrip("/")
        self.persist_to_db = persist_to_db
        # Use a separate thread for sending logs to avoid blocking main execution?
        # For now, we'll use asyncio.create_task if there is a running loop, 
        # or fire-and-forget logic.
        
    def emit(self, record: logging.LogRecord):
        try:
            # We only want to forward logs that are likely "important" or if explicitly configured
            # But let's forward everything that passes the logger's level filter
            
            # Avoid recursion if logging from within this handler's dependencies
            if getattr(record, "_cls_forwarding", False):
                return

            log_entry = self._prepare_log_entry(record)
            
            # Send asynchronously
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    loop.create_task(self._send_log(log_entry))
                else:
                    # If no loop (e.g. startup/shutdown), we might lose logs or need sync send
                    # For safety in this environment, just print a warning or skip
                    pass 
            except RuntimeError:
                # No running loop
                pass
                
        except Exception:
            self.handleError(record)

    def _prepare_log_entry(self, record: logging.LogRecord) -> Dict[str, Any]:
        # Extract context
        c_id = correlation_id.get() or str(uuid.uuid4())
        
        # Format message
        msg = self.format(record)
        
        return {
            "trace_id": c_id,
            "span_id": request_id.get() or "root",
            "service": self.service_name,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "log_type": "TECHNICAL", # Standard logs are technical
            "level": record.levelname,
            "payload": {"message": record.getMessage(), "logger": record.name},
            "context": {
                "module": record.module,
                "function": record.funcName,
                "line": record.lineno,
                "filename": record.filename
            },
            "metadata": {
                "persist_to_db": str(self.persist_to_db).lower(),
                "environment": os.getenv("ENVIRONMENT", "development")
            }
        }

    async def _send_log(self, log_entry: Dict[str, Any]):
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                await client.post(f"{self.cls_url}/logs", json=log_entry)
        except Exception:
            # Silent fail for fire-and-forget
            pass


class EnhancedJSONFormatter(logging.Formatter):
    """Enhanced JSON formatter with correlation IDs and performance metrics"""
    
    def __init__(self, service_name: str = "am-service", config: LogConfig = None):
        super().__init__()
        self.service_name = service_name
        self.config = config or LogConfig()
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record as enhanced JSON"""
        # Base log entry
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service_name,
            "environment": self.config.environment,
            "module": getattr(record, 'module', ''),
            "function": getattr(record, 'funcName', ''),
            "line": getattr(record, 'lineno', ''),
            "filename": getattr(record, 'filename', ''),
            "path": getattr(record, 'pathname', ''),
            "thread": getattr(record, 'thread', ''),
            "process": getattr(record, 'process', ''),
        }
        
        # Add correlation context
        if self.config.enable_correlation:
            log_entry.update({
                "correlation_id": correlation_id.get(),
                "request_id": request_id.get(),
                "user_id": user_id.get(),
                "service_context": service_name.get() or self.service_name
            })
        
        # Add performance metrics if available
        if hasattr(record, 'duration_ms'):
            log_entry["duration_ms"] = record.duration_ms
        if hasattr(record, 'performance_data'):
            log_entry["performance"] = record.performance_data
        
        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "traceback": self.formatException(record.exc_info)
            }
        
        # Add extra fields from record (custom context)
        excluded_fields = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "lineno", "funcName", "created",
            "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "message", "exc_info", "exc_text",
            "stack_info", "duration_ms", "performance_data"
        }
        
        for key, value in record.__dict__.items():
            if key not in excluded_fields and not key.startswith('_'):
                # Handle complex objects
                try:
                    json.dumps(value)  # Test if serializable
                    log_entry[key] = value
                except (TypeError, ValueError):
                    log_entry[key] = str(value)
        
        return json.dumps(log_entry, default=str, ensure_ascii=False)


class StructuredFormatter(logging.Formatter):
    """Human-readable structured formatter"""
    
    def __init__(self, service_name: str = "am-service", config: LogConfig = None):
        super().__init__()
        self.service_name = service_name
        self.config = config or LogConfig()
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record in structured text format"""
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        
        # Base format
        base_msg = (
            f"[{timestamp}] {record.levelname:8} "
            f"{self.service_name}:{record.name} - {record.getMessage()}"
        )
        
        # Add context if available
        context_parts = []
        if self.config.enable_correlation:
            if correlation_id.get():
                context_parts.append(f"correlation={correlation_id.get()}")
            if user_id.get():
                context_parts.append(f"user={user_id.get()}")
        
        if hasattr(record, 'duration_ms'):
            context_parts.append(f"duration={record.duration_ms}ms")
        
        if context_parts:
            base_msg += f" [{', '.join(context_parts)}]"
        
        # Add location info in debug mode
        if record.levelno <= logging.DEBUG:
            base_msg += f" ({getattr(record, 'source_module', record.module if hasattr(record, 'module') else '')}:{record.funcName}:{record.lineno})"
        
        # Add exception if present
        if record.exc_info:
            base_msg += "\n" + self.formatException(record.exc_info)
        
        return base_msg


class PerformanceFilter(logging.Filter):
    """Filter to add performance metrics to log records"""
    
    def filter(self, record: logging.LogRecord) -> bool:
        """Add performance context to log record"""
        # Add request start time if not present
        if not hasattr(record, 'request_start_time'):
            record.request_start_time = time.time()
        
        return True


class SamplingFilter(logging.Filter):
    """Filter for log sampling to reduce volume in high-traffic scenarios"""
    
    def __init__(self, sampling_rate: float = 1.0, sample_debug_only: bool = True):
        super().__init__()
        self.sampling_rate = sampling_rate
        self.sample_debug_only = sample_debug_only
        self._counter = 0
        self._lock = threading.Lock()
    
    def filter(self, record: logging.LogRecord) -> bool:
        """Sample log records based on configured rate"""
        # Always allow non-debug logs unless specifically configured
        if self.sample_debug_only and record.levelno > logging.DEBUG:
            return True
        
        # Sample based on rate
        if self.sampling_rate >= 1.0:
            return True
        
        with self._lock:
            self._counter += 1
            should_log = (self._counter % int(1 / self.sampling_rate)) == 0
            return should_log


class AMLogger:
    """Advanced centralized logger for AM Portfolio services"""
    
    def __init__(self, config: LogConfig):
        self.config = config
        self.loggers: Dict[str, logging.Logger] = {}
        self._setup_root_logger()
    
    def _setup_root_logger(self) -> None:
        """Setup the root logger configuration"""
        root_logger = logging.getLogger()
        root_logger.setLevel(getattr(logging, self.config.log_level.upper()))
        
        # Clear existing handlers
        root_logger.handlers.clear()
        
        # Create formatters
        if self.config.log_format.lower() == "json":
            formatter = EnhancedJSONFormatter(self.config.service_name, self.config)
        elif self.config.log_format.lower() == "structured":
            formatter = StructuredFormatter(self.config.service_name, self.config)
        else:
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
        
        # Console handler
        if self.config.console_output:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(formatter)
            
            # Add filters
            if self.config.enable_performance:
                console_handler.addFilter(PerformanceFilter())
            
            if self.config.enable_sampling and self.config.sampling_rate < 1.0:
                console_handler.addFilter(SamplingFilter(self.config.sampling_rate))
            
            root_logger.addHandler(console_handler)
        
        # File handler
        if self.config.file_output and self.config.file_path:
            self._setup_file_handler(root_logger, formatter)
        
        # Configure third-party loggers
        self._configure_third_party_loggers()
    
    def _setup_file_handler(self, root_logger: logging.Logger, formatter: logging.Formatter) -> None:
        """Setup file logging with rotation"""
        # Ensure log directory exists
        log_path = Path(self.config.file_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        if self.config.rotation_type == "time":
            file_handler = TimedRotatingFileHandler(
                self.config.file_path,
                when=self.config.time_rotation,
                backupCount=self.config.backup_count,
                encoding="utf-8"
            )
        else:
            file_handler = RotatingFileHandler(
                self.config.file_path,
                maxBytes=self.config.max_file_size,
                backupCount=self.config.backup_count,
                encoding="utf-8"
            )
        
        file_handler.setFormatter(formatter)
        
        # Add filters
        if self.config.enable_performance:
            file_handler.addFilter(PerformanceFilter())
        
        if self.config.enable_sampling and self.config.sampling_rate < 1.0:
            file_handler.addFilter(SamplingFilter(self.config.sampling_rate))
        
        root_logger.addHandler(file_handler)
    
    def _configure_third_party_loggers(self) -> None:
        """Configure log levels and handlers for third-party libraries"""
        third_party_configs = {
            "uvicorn": logging.INFO,
            "uvicorn.access": logging.INFO,
            "uvicorn.error": logging.INFO,
            "fastapi": logging.INFO,
            "sqlalchemy.engine": logging.WARNING,
            "sqlalchemy.dialects": logging.WARNING,
            "sqlalchemy.pool": logging.WARNING,
            "asyncio": logging.WARNING,
            "aiohttp": logging.INFO,
            "httpx": logging.WARNING,
            "urllib3": logging.WARNING,
        }
        
        # Get handlers from root logger to reuse
        root_logger = logging.getLogger()
        handlers = root_logger.handlers
        
        for logger_name, level in third_party_configs.items():
            logger = logging.getLogger(logger_name)
            logger.setLevel(level)
            
            # For Uvicorn, we want to override handlers to use our formatter
            if logger_name.startswith("uvicorn"):
                logger.handlers = []
                # Don't propagate if we attach handlers directly, but here we reuse root handlers
                # If we propagate, it goes to root. 
                # Uvicorn often sets up its own handlers, so we clear them.
                # We can either set propagate=True (and rely on root) or attach handlers.
                logger.propagate = True
                for handler in handlers:
                    logger.addHandler(handler)
    
    def get_logger(self, name: Optional[str] = None) -> logging.Logger:
        """Get or create a logger instance"""
        logger_name = name or self.config.service_name
        
        if logger_name not in self.loggers:
            logger = logging.getLogger(logger_name)
            self.loggers[logger_name] = logger
        
        return self.loggers[logger_name]
    
    def set_correlation_context(
        self,
        correlation_id_val: Optional[str] = None,
        request_id_val: Optional[str] = None,
        user_id_val: Optional[str] = None,
        service_name_val: Optional[str] = None
    ) -> None:
        """Set correlation context for current request"""
        if correlation_id_val:
            correlation_id.set(correlation_id_val)
        if request_id_val:
            request_id.set(request_id_val)
        if user_id_val:
            user_id.set(user_id_val)
        if service_name_val:
            service_name.set(service_name_val)
    
    def clear_correlation_context(self) -> None:
        """Clear correlation context"""
        correlation_id.set(None)
        request_id.set(None)
        user_id.set(None)
        service_name.set(None)
    
    def generate_correlation_id(self) -> str:
        """Generate a new correlation ID"""
        return str(uuid.uuid4())


# Global logger instance (will be initialized by setup_logging)
_global_logger: Optional[AMLogger] = None


def setup_logging(config: LogConfig) -> AMLogger:
    """Setup global logging configuration"""
    global _global_logger
    _global_logger = AMLogger(config)
    return _global_logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Get logger instance (convenience function)"""
    if _global_logger is None:
        # Fallback configuration
        config = LogConfig()
        setup_logging(config)
    
    return _global_logger.get_logger(name)


def set_correlation_context(
    correlation_id_val: Optional[str] = None,
    request_id_val: Optional[str] = None,
    user_id_val: Optional[str] = None,
    service_name_val: Optional[str] = None
) -> None:
    """Set correlation context (convenience function)"""
    if _global_logger:
        _global_logger.set_correlation_context(
            correlation_id_val, request_id_val, user_id_val, service_name_val
        )


def clear_correlation_context() -> None:
    """Clear correlation context (convenience function)"""
    if _global_logger:
        _global_logger.clear_correlation_context()


def generate_correlation_id() -> str:
    """Generate new correlation ID (convenience function)"""
    if _global_logger:
        return _global_logger.generate_correlation_id()
    return str(uuid.uuid4())


# Decorators for automatic logging
def log_execution_time(logger_name: Optional[str] = None):
    """Decorator to automatically log function execution time"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            logger = get_logger(logger_name or func.__module__)
            
            try:
                result = func(*args, **kwargs)
                duration_ms = (time.time() - start_time) * 1000
                logger.info(
                    f"Function {func.__name__} completed",
                    extra={
                        "duration_ms": round(duration_ms, 2),
                        "function": func.__name__,
                        "source_module": func.__module__
                    }
                )
                return result
            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                logger.error(
                    f"Function {func.__name__} failed: {str(e)}",
                    extra={
                        "duration_ms": round(duration_ms, 2),
                        "function": func.__name__,
                        "source_module": func.__module__,
                        "error": str(e)
                    },
                    exc_info=True
                )
                raise
        return wrapper
    return decorator


def log_async_execution_time(logger_name: Optional[str] = None):
    """Decorator to automatically log async function execution time"""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            start_time = time.time()
            logger = get_logger(logger_name or func.__module__)
            
            try:
                result = await func(*args, **kwargs)
                duration_ms = (time.time() - start_time) * 1000
                logger.info(
                    f"Async function {func.__name__} completed",
                    extra={
                        "duration_ms": round(duration_ms, 2),
                        "function": func.__name__,
                        "source_module": func.__module__
                    }
                )
                return result
            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                logger.error(
                    f"Async function {func.__name__} failed: {str(e)}",
                    extra={
                        "duration_ms": round(duration_ms, 2),
                        "function": func.__name__,
                        "source_module": func.__module__,
                        "error": str(e)
                    },
                    exc_info=True
                )
                raise
        return wrapper
    return decorator


class LoggerMixin:
    """Mixin class to add advanced logging capabilities to any class"""
    
    @property
    def logger(self) -> logging.Logger:
        """Get logger for this class"""
        return get_logger(f"{self.__class__.__module__}.{self.__class__.__name__}")
    
    def log_info(self, message: str, **kwargs) -> None:
        """Log info message with context"""
        self.logger.info(message, extra=kwargs)
    
    def log_warning(self, message: str, **kwargs) -> None:
        """Log warning message with context"""
        self.logger.warning(message, extra=kwargs)
    
    def log_error(self, message: str, exc_info: bool = False, **kwargs) -> None:
        """Log error message with context"""
        self.logger.error(message, exc_info=exc_info, extra=kwargs)
    
    def log_debug(self, message: str, **kwargs) -> None:
        """Log debug message with context"""
        self.logger.debug(message, extra=kwargs)
    
    def log_with_timing(self, message: str, start_time: float, **kwargs) -> None:
        """Log message with execution timing"""
        duration_ms = (time.time() - start_time) * 1000
        kwargs['duration_ms'] = round(duration_ms, 2)
        self.logger.info(message, extra=kwargs)