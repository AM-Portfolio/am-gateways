"""
FastAPI middleware for automatic logging correlation and request tracking
"""

import time
import uuid
from typing import Callable, Optional
from fastapi import FastAPI, Request, Response
from fastapi.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from .logger import (
    set_correlation_context,
    clear_correlation_context,
    get_logger,
    generate_correlation_id
)


class LoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware to automatically handle:
    - Correlation ID generation/propagation
    - Request/response logging
    - Performance timing
    - Error tracking
    """
    
    def __init__(
        self,
        app: ASGIApp,
        service_name: str = "am-service",
        log_requests: bool = True,
        log_responses: bool = True,
        correlation_header: str = "X-Correlation-ID",
        request_id_header: str = "X-Request-ID",
        user_id_header: str = "X-User-ID",
        exclude_paths: Optional[list] = None
    ):
        super().__init__(app)
        self.service_name = service_name
        self.log_requests = log_requests
        self.log_responses = log_responses
        self.correlation_header = correlation_header
        self.request_id_header = request_id_header
        self.user_id_header = user_id_header
        self.exclude_paths = exclude_paths or ["/health", "/metrics", "/docs", "/openapi.json"]
        self.logger = get_logger(f"{service_name}.middleware")
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and response with logging"""
        start_time = time.time()
        
        # Skip logging for excluded paths
        if any(request.url.path.startswith(path) for path in self.exclude_paths):
            return await call_next(request)
        
        # Extract or generate correlation context
        correlation_id_val = (
            request.headers.get(self.correlation_header) or 
            generate_correlation_id()
        )
        request_id_val = (
            request.headers.get(self.request_id_header) or
            str(uuid.uuid4())
        )
        user_id_val = request.headers.get(self.user_id_header)
        
        # Set correlation context
        set_correlation_context(
            correlation_id_val=correlation_id_val,
            request_id_val=request_id_val,
            user_id_val=user_id_val,
            service_name_val=self.service_name
        )
        
        try:
            # Log incoming request
            if self.log_requests:
                await self._log_request(request, correlation_id_val, request_id_val)
            
            # Process request
            response = await call_next(request)
            
            # Calculate timing
            duration_ms = (time.time() - start_time) * 1000
            
            # Add correlation headers to response
            response.headers[self.correlation_header] = correlation_id_val
            response.headers[self.request_id_header] = request_id_val
            
            # Log response
            if self.log_responses:
                await self._log_response(
                    request, response, duration_ms, correlation_id_val, request_id_val
                )
            
            return response
        
        except Exception as e:
            # Calculate timing for error case
            duration_ms = (time.time() - start_time) * 1000
            
            # Log error
            self.logger.error(
                f"Request failed: {str(e)}",
                extra={
                    "method": request.method,
                    "url": str(request.url),
                    "duration_ms": round(duration_ms, 2),
                    "correlation_id": correlation_id_val,
                    "request_id": request_id_val,
                    "user_id": user_id_val,
                    "error": str(e),
                    "error_type": type(e).__name__
                },
                exc_info=True
            )
            raise
        
        finally:
            # Clear correlation context
            clear_correlation_context()
    
    async def _log_request(self, request: Request, correlation_id_val: str, request_id_val: str) -> None:
        """Log incoming request details"""
        # Get request body size if available
        content_length = request.headers.get("content-length", "0")
        
        # Extract client info
        client_ip = self._get_client_ip(request)
        user_agent = request.headers.get("user-agent", "")
        
        self.logger.info(
            f"Incoming {request.method} {request.url.path}",
            extra={
                "method": request.method,
                "url": str(request.url),
                "path": request.url.path,
                "query_params": dict(request.query_params),
                "client_ip": client_ip,
                "user_agent": user_agent,
                "content_length": content_length,
                "correlation_id": correlation_id_val,
                "request_id": request_id_val,
                "headers": dict(request.headers) if len(request.headers) < 20 else "truncated"
            }
        )
    
    async def _log_response(
        self,
        request: Request,
        response: Response,
        duration_ms: float,
        correlation_id_val: str,
        request_id_val: str
    ) -> None:
        """Log response details"""
        # Determine log level based on status code
        if response.status_code >= 500:
            log_level = "error"
        elif response.status_code >= 400:
            log_level = "warning"
        else:
            log_level = "info"
        
        log_message = (
            f"Response {response.status_code} for {request.method} {request.url.path} "
            f"({duration_ms:.2f}ms)"
        )
        
        log_extra = {
            "method": request.method,
            "url": str(request.url),
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round(duration_ms, 2),
            "correlation_id": correlation_id_val,
            "request_id": request_id_val,
            "response_headers": dict(response.headers) if len(response.headers) < 10 else "truncated"
        }
        
        # Add performance categorization
        if duration_ms > 5000:
            log_extra["performance"] = "very_slow"
        elif duration_ms > 1000:
            log_extra["performance"] = "slow"
        elif duration_ms > 500:
            log_extra["performance"] = "moderate"
        else:
            log_extra["performance"] = "fast"
        
        getattr(self.logger, log_level)(log_message, extra=log_extra)
    
    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP address from request"""
        # Check for forwarded headers (common in load balancer setups)
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip
        
        # Fallback to direct client
        if hasattr(request, "client") and request.client:
            return request.client.host
        
        return "unknown"


def setup_fastapi_logging(
    app: FastAPI,
    service_name: str = "am-service",
    log_requests: bool = True,
    log_responses: bool = True,
    correlation_header: str = "X-Correlation-ID",
    request_id_header: str = "X-Request-ID",
    user_id_header: str = "X-User-ID",
    exclude_paths: Optional[list] = None
) -> None:
    """
    Setup FastAPI application with logging middleware
    
    Args:
        app: FastAPI application instance
        service_name: Name of the service for logging context
        log_requests: Whether to log incoming requests
        log_responses: Whether to log responses
        correlation_header: Header name for correlation ID
        request_id_header: Header name for request ID
        user_id_header: Header name for user ID
        exclude_paths: List of paths to exclude from logging
    """
    middleware = LoggingMiddleware(
        app=app,
        service_name=service_name,
        log_requests=log_requests,
        log_responses=log_responses,
        correlation_header=correlation_header,
        request_id_header=request_id_header,
        user_id_header=user_id_header,
        exclude_paths=exclude_paths
    )
    
    app.add_middleware(LoggingMiddleware, **{
        "service_name": service_name,
        "log_requests": log_requests,
        "log_responses": log_responses,
        "correlation_header": correlation_header,
        "request_id_header": request_id_header,
        "user_id_header": user_id_header,
        "exclude_paths": exclude_paths
    })