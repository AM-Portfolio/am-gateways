"""
Shared JWT validation utilities for internal services
"""
import os
import jwt
from jwt.exceptions import InvalidTokenError, ExpiredSignatureError
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from fastapi import HTTPException, status


class JWTValidator:
    """JWT token validation utility"""
    
    def __init__(self):
        self.user_jwt_secret = os.getenv("JWT_SECRET", "your-secret-key-here-change-in-production")
        self.internal_jwt_secret = os.getenv("INTERNAL_JWT_SECRET", "internal-service-super-secret-key-32chars-minimum-change-in-prod")
        self.algorithm = "HS256"
    
    def validate_user_token(self, token: str) -> Dict[str, Any]:
        """Validate user JWT token"""
        try:
            payload = jwt.decode(token, self.user_jwt_secret, algorithms=[self.algorithm])
            
            if payload.get("type") != "user_token":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid token type"
                )
            
            return payload
        except ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired"
            )
        except InvalidTokenError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Token validation failed: {str(e)}"
            )
    
    def validate_service_token(self, token: str) -> Dict[str, Any]:
        """Validate service JWT token"""
        try:
            payload = jwt.decode(token, self.internal_jwt_secret, algorithms=[self.algorithm])
            
            if payload.get("type") != "service_token":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid token type"
                )
            
            return payload
        except ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired"
            )
        except InvalidTokenError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Token validation failed: {str(e)}"
            )
    
    def validate_any_token(self, token: str) -> tuple[str, Dict[str, Any]]:
        """Validate either user or service token and return type and payload"""
        try:
            # Try user token first
            payload = jwt.decode(token, self.user_jwt_secret, algorithms=[self.algorithm])
            # User tokens from User Management have 'sub' (user_id) and 'email' fields
            # They don't have 'type' field, so we check for these identifying fields
            if payload.get("sub") and payload.get("email"):
                # Add user_id field for backward compatibility
                payload["user_id"] = payload.get("sub")
                return "user", payload
            # Legacy format with explicit type field
            if payload.get("type") == "user_token":
                return "user", payload
        except InvalidTokenError:
            pass
        
        try:
            # Try service token
            payload = jwt.decode(token, self.internal_jwt_secret, algorithms=[self.algorithm])
            if payload.get("type") == "service_token":
                return "service", payload
        except InvalidTokenError:
            pass
        
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )
    
    def generate_service_token(self, service_id: str, service_name: str, 
                             permissions: list = None, expiry_minutes: int = 60) -> str:
        """Generate service-to-service token"""
        now = datetime.utcnow()
        payload = {
            "service_id": service_id,
            "service_name": service_name,
            "type": "service_token",
            "permissions": permissions or [],
            "exp": now + timedelta(minutes=expiry_minutes),
            "iat": now,
            "iss": "am-auth-tokens"
        }
        
        return jwt.encode(payload, self.internal_jwt_secret, algorithm=self.algorithm)


# Global validator instance
jwt_validator = JWTValidator()