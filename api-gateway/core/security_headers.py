"""Security response headers middleware for AM API Gateway."""
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request
import logging

logger = logging.getLogger(__name__)

# Headers stamped on every outbound response
_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Cache-Control": "no-store",
    "X-Powered-By": "",  # remove server fingerprint
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Stamps hardened security headers on every HTTP response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            if value:
                response.headers[header] = value
            elif header in response.headers:
                del response.headers[header]
        return response
