import os
from .am_logger_core import AMLogger, audit_activity

# Configuration for Centralized Logging
CLS_URL = os.getenv("CLS_URL", "http://am-logging-svc.infra.svc.cluster.local")
SERVICE_NAME = os.getenv("AM_SERVICE_NAME", "am-auth")

# Singleton instance of the Enterprise Logger
am_logger = AMLogger(service_name=SERVICE_NAME, cls_url=CLS_URL)

def get_am_logger():
    return am_logger

# Re-export the decorator for easy use
audit_logger = audit_activity(am_logger)
