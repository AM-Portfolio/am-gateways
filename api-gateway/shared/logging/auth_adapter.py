"""
AM Auth Service Logging Adapter
Integrates AM Logging SDK with the Auth service for fire-and-forget logging
"""

import asyncio
import uuid
import inspect
from typing import Dict, Any, Optional
from contextlib import asynccontextmanager

from .am_logging_sdk import AMLoggingClient, LoggerMixin


class AMAuthLogger(LoggerMixin):
    """Specialized logger for AM Auth services with fire-and-forget capability"""
    
    def __init__(self, service_name: str = "am-auth", base_url: str = "http://am-logging-svc/v1", persist_to_db: Optional[bool] = None):
        super().__init__()
        self._log_client = AMLoggingClient(base_url=base_url, persist_to_db=persist_to_db)
        self._service_name = service_name
        self._context_stack = []
    
    @asynccontextmanager
    async def trace_context(self, operation: str, trace_id: Optional[str] = None):
        """Create a trace context for operation tracking"""
        trace_id = trace_id or str(uuid.uuid4())
        span_id = str(uuid.uuid4())
        
        context = {
            "trace_id": trace_id,
            "span_id": span_id,
            "operation": operation,
            "start_time": asyncio.get_event_loop().time()
        }
        
        self._context_stack.append(context)
        
        try:
            self.log_info(f"Starting operation: {operation}", trace_id=trace_id, span_id=span_id)
            yield context
        except Exception as e:
            self.log_error(f"Operation failed: {operation}", 
                          trace_id=trace_id, span_id=span_id, 
                          error=str(e), exception={"type": type(e).__name__, "message": str(e)})
            raise
        finally:
            end_time = asyncio.get_event_loop().time()
            latency_ms = round((end_time - context["start_time"]) * 1000, 2)
            self.log_info(f"Completed operation: {operation}", 
                          trace_id=trace_id, span_id=span_id, 
                          latency_ms=latency_ms)
            self._context_stack.pop()
    
    def get_current_context(self) -> Optional[Dict[str, Any]]:
        """Get current trace context if available"""
        return self._context_stack[-1] if self._context_stack else None
    
    def log_auth_event(self, event_type: str, user_id: Optional[str] = None, **kwargs):
        """Log authentication-related events"""
        context = self.get_current_context()
        extra_data = {
            "event_type": event_type,
            "user_id": user_id,
            **kwargs
        }
        
        if context:
            extra_data.update({
                "trace_id": context["trace_id"],
                "span_id": context["span_id"],
                "operation": context["operation"]
            })
        
        self.log_info(f"Auth event: {event_type}", **extra_data)
    
    def log_security_event(self, event_type: str, severity: str = "MEDIUM", **kwargs):
        """Log security-related events"""
        context = self.get_current_context()
        extra_data = {
            "event_type": event_type,
            "security_severity": severity,
            **kwargs
        }
        
        if context:
            extra_data.update({
                "trace_id": context["trace_id"],
                "span_id": context["span_id"],
                "operation": context["operation"]
            })
        
        level = "ERROR" if severity in ["HIGH", "CRITICAL"] else "WARN"
        getattr(self, f"log_{level.lower()}")(f"Security event: {event_type}", **extra_data)


class AuthLoggingMixin:
    """Mixin to add AM Auth logging capabilities to any service class"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.auth_logger = AMAuthLogger(
            service_name=getattr(self, 'service_name', 'am-auth')
        )
    
    async def with_tracing(self, operation: str, func, *args, **kwargs):
        """Execute a function with automatic tracing"""
        async with self.auth_logger.trace_context(operation) as context:
            try:
                if asyncio.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)
                return result
            except Exception as e:
                self.auth_logger.log_error(f"Exception in {operation}", 
                                         trace_id=context["trace_id"],
                                         span_id=context["span_id"],
                                         exception={"type": type(e).__name__, "message": str(e)})
                raise


# Global logger instance for the auth service
auth_logger = AMAuthLogger()


def get_auth_logger() -> AMAuthLogger:
    """Get the global auth logger instance"""
    return auth_logger


def log_user_login(user_id: str, success: bool = True, **kwargs):
    """Convenience function for logging user login attempts"""
    auth_logger.log_auth_event(
        "USER_LOGIN",
        user_id=user_id,
        success=success,
        **kwargs
    )


def log_user_logout(user_id: str, **kwargs):
    """Convenience function for logging user logout"""
    auth_logger.log_auth_event(
        "USER_LOGOUT", 
        user_id=user_id,
        **kwargs
    )


def log_token_generated(user_id: str, token_type: str, **kwargs):
    """Convenience function for logging token generation"""
    auth_logger.log_auth_event(
        "TOKEN_GENERATED",
        user_id=user_id,
        token_type=token_type,
        **kwargs
    )


def log_security_violation(violation_type: str, user_id: Optional[str] = None, **kwargs):
    """Convenience function for logging security violations"""
    auth_logger.log_security_event(
        violation_type,
        severity="HIGH",
        user_id=user_id,
        **kwargs
    )
