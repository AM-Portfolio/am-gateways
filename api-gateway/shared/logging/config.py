"""
Configuration management for centralized logging
"""

import os
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
from pathlib import Path

from .logger import LogConfig


@dataclass
class ServiceLogConfig:
    """Service-specific logging configuration"""
    service_name: str
    log_level: str = "INFO"
    enable_file_logging: bool = False
    log_file_path: Optional[str] = None
    custom_settings: Dict[str, Any] = field(default_factory=dict)


class LogConfigManager:
    """Manages logging configuration for different AM services"""
    
    def __init__(self):
        self.service_configs: Dict[str, ServiceLogConfig] = {}
        self._load_default_configs()
    
    def _load_default_configs(self) -> None:
        """Load default configurations for AM services"""
        
        # Auth Tokens Service Configuration
        auth_tokens_config = ServiceLogConfig(
            service_name="am-auth-tokens",
            log_level=os.getenv("AUTH_TOKENS_LOG_LEVEL", "INFO"),
            enable_file_logging=os.getenv("AUTH_TOKENS_FILE_LOGGING", "false").lower() == "true",
            log_file_path=os.getenv("AUTH_TOKENS_LOG_FILE", "/var/log/am-auth-tokens/app.log"),
            custom_settings={
                "enable_oauth_debug": os.getenv("AUTH_TOKENS_OAUTH_DEBUG", "false").lower() == "true",
                "enable_jwt_debug": os.getenv("AUTH_TOKENS_JWT_DEBUG", "false").lower() == "true",
                "log_token_events": os.getenv("AUTH_TOKENS_LOG_EVENTS", "true").lower() == "true",
                "persist_to_db": os.getenv("AUTH_TOKENS_PERSIST_TO_DB", "false").lower() == "true"
            }
        )
        
        # User Management Service Configuration  
        user_mgmt_config = ServiceLogConfig(
            service_name="am-user-management",
            log_level=os.getenv("USER_MGMT_LOG_LEVEL", "INFO"),
            enable_file_logging=os.getenv("USER_MGMT_FILE_LOGGING", "false").lower() == "true",
            log_file_path=os.getenv("USER_MGMT_LOG_FILE", "/var/log/am-user-management/app.log"),
            custom_settings={
                "enable_db_query_logging": os.getenv("USER_MGMT_DB_DEBUG", "false").lower() == "true",
                "log_user_events": os.getenv("USER_MGMT_LOG_EVENTS", "true").lower() == "true",
                "enable_email_debug": os.getenv("USER_MGMT_EMAIL_DEBUG", "false").lower() == "true",
                "persist_to_db": os.getenv("USER_MGMT_PERSIST_TO_DB", "true").lower() == "true"
            }
        )
        
        self.service_configs["am-auth-tokens"] = auth_tokens_config
        self.service_configs["am-user-management"] = user_mgmt_config
    
    def get_config_for_service(self, service_name: str) -> LogConfig:
        """Get LogConfig for a specific service"""
        service_config = self.service_configs.get(service_name)
        
        if not service_config:
            # Create default config for unknown service
            service_config = ServiceLogConfig(
                service_name=service_name,
                log_level=os.getenv("DEFAULT_LOG_LEVEL", "INFO")
            )
        
        # Build LogConfig from service config and environment
        return LogConfig(
            service_name=service_config.service_name,
            log_level=service_config.log_level,
            log_format=os.getenv("LOG_FORMAT", "json"),
            console_output=os.getenv("LOG_CONSOLE", "true").lower() == "true",
            file_output=service_config.enable_file_logging,
            file_path=service_config.log_file_path if service_config.enable_file_logging else None,
            max_file_size=int(os.getenv("LOG_MAX_FILE_SIZE", "10485760")),  # 10MB
            backup_count=int(os.getenv("LOG_BACKUP_COUNT", "5")),
            rotation_type=os.getenv("LOG_ROTATION_TYPE", "size"),
            enable_correlation=os.getenv("LOG_ENABLE_CORRELATION", "true").lower() == "true",
            enable_performance=os.getenv("LOG_ENABLE_PERFORMANCE", "true").lower() == "true",
            enable_sampling=os.getenv("LOG_ENABLE_SAMPLING", "false").lower() == "true",
            sampling_rate=float(os.getenv("LOG_SAMPLING_RATE", "1.0")),
            environment=os.getenv("ENVIRONMENT", "development"),
            persist_to_db=service_config.custom_settings.get("persist_to_db", os.getenv("LOG_PERSIST_TO_DB", "false").lower() == "true")
        )
    
    def register_service(self, config: ServiceLogConfig) -> None:
        """Register a new service configuration"""
        self.service_configs[config.service_name] = config
    
    def update_service_config(self, service_name: str, **kwargs) -> None:
        """Update configuration for a service"""
        if service_name in self.service_configs:
            config = self.service_configs[service_name]
            for key, value in kwargs.items():
                if hasattr(config, key):
                    setattr(config, key, value)
                else:
                    config.custom_settings[key] = value


# Global configuration manager instance
config_manager = LogConfigManager()


def get_service_log_config(service_name: str) -> LogConfig:
    """Convenience function to get log config for a service"""
    return config_manager.get_config_for_service(service_name)


def setup_auth_tokens_logging() -> LogConfig:
    """Setup logging configuration specifically for auth-tokens service"""
    config = get_service_log_config("am-auth-tokens")
    
    # Create log directory if file logging is enabled
    if config.file_output and config.file_path:
        log_path = Path(config.file_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
    
    return config


def setup_user_management_logging() -> LogConfig:
    """Setup logging configuration specifically for user-management service"""
    config = get_service_log_config("am-user-management")
    
    # Create log directory if file logging is enabled
    if config.file_output and config.file_path:
        log_path = Path(config.file_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
    
    return config


# Environment variable documentation
ENV_VARS_DOCUMENTATION = """
Centralized Logging Environment Variables:

Global Settings:
- LOG_FORMAT: json|structured|text (default: json)
- LOG_CONSOLE: true|false (default: true)
- LOG_MAX_FILE_SIZE: bytes (default: 10485760 = 10MB)
- LOG_BACKUP_COUNT: number (default: 5)
- LOG_ROTATION_TYPE: size|time (default: size)
- LOG_ENABLE_CORRELATION: true|false (default: true)
- LOG_ENABLE_PERFORMANCE: true|false (default: true)
- LOG_ENABLE_SAMPLING: true|false (default: false)
- LOG_SAMPLING_RATE: float 0.0-1.0 (default: 1.0)
- ENVIRONMENT: development|staging|production (default: development)

Auth Tokens Service:
- AUTH_TOKENS_LOG_LEVEL: DEBUG|INFO|WARNING|ERROR (default: INFO)
- AUTH_TOKENS_FILE_LOGGING: true|false (default: false)
- AUTH_TOKENS_LOG_FILE: path (default: /var/log/am-auth-tokens/app.log)
- AUTH_TOKENS_OAUTH_DEBUG: true|false (default: false)
- AUTH_TOKENS_JWT_DEBUG: true|false (default: false)
- AUTH_TOKENS_LOG_EVENTS: true|false (default: true)

User Management Service:
- USER_MGMT_LOG_LEVEL: DEBUG|INFO|WARNING|ERROR (default: INFO)
- USER_MGMT_FILE_LOGGING: true|false (default: false)
- USER_MGMT_LOG_FILE: path (default: /var/log/am-user-management/app.log)
- USER_MGMT_DB_DEBUG: true|false (default: false)
- USER_MGMT_LOG_EVENTS: true|false (default: true)
- USER_MGMT_EMAIL_DEBUG: true|false (default: false)
"""