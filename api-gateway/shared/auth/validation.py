"""
Validation utilities for internal services
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from .jwt_utils import jwt_validator
from typing import Dict, Any


# Security scheme
security = HTTPBearer()


async def validate_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> tuple[str, Dict[str, Any]]:
    """Validate JWT token and return token type and payload"""
    token = credentials.credentials
    return jwt_validator.validate_any_token(token)


async def validate_user_token(auth_info: tuple = Depends(validate_token)) -> Dict[str, Any]:
    """Ensure token is from a user (not service-to-service)"""
    token_type, payload = auth_info
    if token_type != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User token required"
        )
    return payload


async def validate_service_token(auth_info: tuple = Depends(validate_token)) -> Dict[str, Any]:
    """Ensure token is from a service"""
    token_type, payload = auth_info
    if token_type != "service":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service token required"
        )
    return payload


async def validate_user_or_service_token(auth_info: tuple = Depends(validate_token)) -> Dict[str, Any]:
    """Allow both user and service tokens"""
    token_type, payload = auth_info
    payload["_token_type"] = token_type  # Add token type to payload for downstream use
    return payload