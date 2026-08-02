"""
Fire-and-forget logging configuration for AM services
Ensures non-blocking log sending with proper async handling
"""

import asyncio
import logging
from typing import Dict, Any, Optional
from contextlib import asynccontextmanager

from .am_logging_sdk import AMLoggingClient


class FireAndForgetLoggingHandler:
    """Handler for fire-and-forget logging with proper async management"""
    
    def __init__(self, base_url: str = "http://am-logging-svc/v1", timeout: float = 2.0):
        self.client = AMLoggingClient(base_url=base_url, timeout=timeout)
        self._background_tasks = set()
        self._shutdown_event = asyncio.Event()
        
    async def send_log_async(self, log_entry: Dict[str, Any]) -> None:
        """Send log asynchronously without waiting for response"""
        if self._shutdown_event.is_set():
            return
            
        task = asyncio.create_task(self._send_with_timeout(log_entry))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
    
    async def _send_with_timeout(self, log_entry: Dict[str, Any]) -> None:
        """Send log with timeout and error handling"""
        try:
            await asyncio.wait_for(self.client._send_log_async(log_entry), timeout=2.0)
        except asyncio.TimeoutError:
            # Silent fail for fire-and-forget
            pass
        except Exception:
            # Silent fail for fire-and-forget
            pass
    
    def send_log_sync(self, log_entry: Dict[str, Any]) -> None:
        """Fire-and-forget from sync context"""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.send_log_async(log_entry))
        except RuntimeError:
            # No event loop running, create one
            asyncio.run(self.send_log_async(log_entry))
    
    async def shutdown(self, timeout: float = 5.0) -> None:
        """Gracefully shutdown pending log tasks"""
        self._shutdown_event.set()
        
        if self._background_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._background_tasks, return_exceptions=True),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                # Force shutdown after timeout
                for task in self._background_tasks:
                    task.cancel()
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
        
        self._background_tasks.clear()


class AsyncLoggingMixin:
    """Mixin to add fire-and-forget logging to any class"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fire_and_forget_handler = FireAndForgetLoggingHandler()
        self.service_name = getattr(self, 'service_name', self.__class__.__module__.split('.')[0])
    
    async def log_fire_and_forget(self, level: str, message: str, **kwargs) -> None:
        """Send log without blocking the current operation"""
        import uuid
        import inspect
        
        trace_id = kwargs.get('trace_id', str(uuid.uuid4()))
        span_id = kwargs.get('span_id', str(uuid.uuid4()))
        
        frame = inspect.currentframe().f_back
        class_name = frame.f_locals.get('self', None).__class__.__name__ if 'self' in frame.f_locals else "Global"
        method_name = frame.f_code.co_name
        
        log_entry = self._fire_and_forget_handler.client.create_log_entry(
            trace_id=trace_id,
            span_id=span_id,
            service=self.service_name,
            level=level,
            payload={"message": message},
            context={
                "class": class_name,
                "method": method_name,
                **{k: v for k, v in kwargs.items() if k not in ['trace_id', 'span_id']}
            }
        )
        
        await self._fire_and_forget_handler.send_log_async(log_entry)
    
    def log_fire_and_forget_sync(self, level: str, message: str, **kwargs) -> None:
        """Send log without blocking from sync context"""
        import uuid
        import inspect
        
        trace_id = kwargs.get('trace_id', str(uuid.uuid4()))
        span_id = kwargs.get('span_id', str(uuid.uuid4()))
        
        frame = inspect.currentframe().f_back
        class_name = frame.f_locals.get('self', None).__class__.__name__ if 'self' in frame.f_locals else "Global"
        method_name = frame.f_code.co_name
        
        log_entry = self._fire_and_forget_handler.client.create_log_entry(
            trace_id=trace_id,
            span_id=span_id,
            service=self.service_name,
            level=level,
            payload={"message": message},
            context={
                "class": class_name,
                "method": method_name,
                **{k: v for k, v in kwargs.items() if k not in ['trace_id', 'span_id']}
            }
        )
        
        self._fire_and_forget_handler.send_log_sync(log_entry)
    
    async def shutdown_logging(self) -> None:
        """Cleanup logging resources"""
        await self._fire_and_forget_handler.shutdown()


# Global fire-and-forget handler
_global_handler = None


def get_fire_and_forget_handler() -> FireAndForgetLoggingHandler:
    """Get or create the global fire-and-forget handler"""
    global _global_handler
    if _global_handler is None:
        _global_handler = FireAndForgetLoggingHandler()
    return _global_handler


async def shutdown_global_logging() -> None:
    """Shutdown the global logging handler"""
    global _global_handler
    if _global_handler:
        await _global_handler.shutdown()
        _global_handler = None


@asynccontextmanager
async def fire_and_forget_context():
    """Context manager for fire-and-forget logging lifecycle"""
    handler = get_fire_and_forget_handler()
    try:
        yield handler
    finally:
        await handler.shutdown()


# Decorator for automatic fire-and-forget logging
def log_fire_and_forget(level: str = "INFO", message: str = None):
    """Decorator to add fire-and-forget logging to functions"""
    def decorator(func):
        if asyncio.iscoroutinefunction(func):
            async def async_wrapper(*args, **kwargs):
                import uuid
                import inspect
                
                trace_id = str(uuid.uuid4())
                span_id = str(uuid.uuid4())
                
                frame = inspect.currentframe().f_back
                class_name = frame.f_locals.get('self', None).__class__.__name__ if 'self' in frame.f_locals else "Global"
                method_name = func.__name__
                
                log_message = message or f"Executing {method_name}"
                
                handler = get_fire_and_forget_handler()
                log_entry = handler.client.create_log_entry(
                    trace_id=trace_id,
                    span_id=span_id,
                    service="am-auth",
                    level=level,
                    payload={"message": log_message},
                    context={
                        "class": class_name,
                        "method": method_name,
                        "function": func.__name__
                    }
                )
                
                await handler.send_log_async(log_entry)
                
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    error_entry = handler.client.create_log_entry(
                        trace_id=trace_id,
                        span_id=span_id,
                        service="am-auth",
                        level="ERROR",
                        payload={"message": f"Error in {method_name}: {str(e)}"},
                        context={
                            "class": class_name,
                            "method": method_name,
                            "function": func.__name__
                        },
                        exception={"type": type(e).__name__, "message": str(e)}
                    )
                    await handler.send_log_async(error_entry)
                    raise
            
            return async_wrapper
        else:
            def sync_wrapper(*args, **kwargs):
                import uuid
                import inspect
                
                trace_id = str(uuid.uuid4())
                span_id = str(uuid.uuid4())
                
                frame = inspect.currentframe().f_back
                class_name = frame.f_locals.get('self', None).__class__.__name__ if 'self' in frame.f_locals else "Global"
                method_name = func.__name__
                
                log_message = message or f"Executing {method_name}"
                
                handler = get_fire_and_forget_handler()
                log_entry = handler.client.create_log_entry(
                    trace_id=trace_id,
                    span_id=span_id,
                    service="am-auth",
                    level=level,
                    payload={"message": log_message},
                    context={
                        "class": class_name,
                        "method": method_name,
                        "function": func.__name__
                    }
                )
                
                handler.send_log_sync(log_entry)
                
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    error_entry = handler.client.create_log_entry(
                        trace_id=trace_id,
                        span_id=span_id,
                        service="am-auth",
                        level="ERROR",
                        payload={"message": f"Error in {method_name}: {str(e)}"},
                        context={
                            "class": class_name,
                            "method": method_name,
                            "function": func.__name__
                        },
                        exception={"type": type(e).__name__, "message": str(e)}
                    )
                    handler.send_log_sync(error_entry)
                    raise
            
            return sync_wrapper
    
    return decorator
