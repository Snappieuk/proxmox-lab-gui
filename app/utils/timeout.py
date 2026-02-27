#!/usr/bin/env python3
"""Timeout decorator and utilities for preventing API call hangs.

Provides configurable timeouts for external API calls to prevent
indefinite blocking on slow Proxmox servers.
"""

import logging
import threading
import functools
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class TimeoutError(Exception):
    """Raised when an operation times out."""
    pass


def timeout_handler(signum, frame):
    """Signal handler for timeout (Unix/Linux only)."""
    raise TimeoutError("Operation timed out")


def with_timeout(timeout_seconds: float = 30) -> Callable:
    """Decorator to add timeout protection to functions (threading-based, cross-platform).
    
    Uses threading instead of signals to support Windows and async contexts.
    
    Args:
        timeout_seconds: Maximum time to wait for function completion (default 30s)
    
    Returns:
        Decorated function that raises TimeoutError if execution exceeds limit
    
    Example:
        @with_timeout(timeout_seconds=10)
        def slow_api_call():
            return proxmox.nodes.list()
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            result = [None]
            exception = [None]
            event = threading.Event()
            
            def target():
                try:
                    result[0] = func(*args, **kwargs)
                except Exception as e:
                    exception[0] = e
                finally:
                    event.set()
            
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            
            # Wait for thread with timeout
            completed = event.wait(timeout=timeout_seconds)
            
            if not completed:
                logger.error(f"Function {func.__name__} exceeded timeout of {timeout_seconds}s")
                raise TimeoutError(f"{func.__name__} exceeded timeout of {timeout_seconds}s")
            
            if exception[0]:
                raise exception[0]
            
            return result[0]
        
        return wrapper
    
    return decorator


def with_timeout_async(timeout_seconds: float = 30) -> Callable:
    """Decorator for async functions with timeout protection.
    
    Uses asyncio.wait_for under the hood.
    
    Args:
        timeout_seconds: Maximum time to wait for async function completion
    
    Returns:
        Decorated async function that raises TimeoutError if execution exceeds limit
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            import asyncio
            try:
                return await asyncio.wait_for(func(*args, **kwargs), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                logger.error(f"Async function {func.__name__} exceeded timeout of {timeout_seconds}s")
                raise TimeoutError(f"{func.__name__} exceeded timeout of {timeout_seconds}s") from None
        
        return wrapper
    
    return decorator


class CircuitBreaker:
    """Circuit breaker pattern for API failures.
    
    Tracks failures and "trips" the circuit to fail-fast instead of
    repeatedly calling failing APIs.
    
    States:
        - CLOSED: Normal operation, calls pass through
        - OPEN: Too many failures, calls fail immediately (fail-fast)
        - HALF_OPEN: Testing if service recovered, limited calls allowed
    
    Example:
        breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60)
        
        try:
            result = breaker.call(proxmox.nodes.list)
        except CircuitBreakerOpen:
            logger.error("API circuit breaker is open, service unavailable")
    """
    
    STATE_CLOSED = "closed"
    STATE_OPEN = "open"
    STATE_HALF_OPEN = "half_open"
    
    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60,
                 expected_exception: type = Exception, name: str = "CircuitBreaker"):
        """Initialize circuit breaker.
        
        Args:
            failure_threshold: Number of failures before tripping circuit
            recovery_timeout: Seconds to wait before attempting recovery
            expected_exception: Exception type to catch (default: all Exceptions)
            name: Name for logging/debugging
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        self.name = name
        
        self.failure_count = 0
        self.last_failure_time = None
        self.state = self.STATE_CLOSED
        self._lock = threading.RLock()
        
        logger.info(f"CircuitBreaker '{name}' initialized: "
                   f"threshold={failure_threshold}, timeout={recovery_timeout}s")
    
    def call(self, func: Callable, *args, **kwargs) -> Any:
        """Call function through circuit breaker.
        
        Args:
            func: Function to call
            *args: Positional arguments for function
            **kwargs: Keyword arguments for function
        
        Returns:
            Function result
        
        Raises:
            CircuitBreakerOpen: If circuit is open (too many failures)
            Exception: Original exception from function if in HALF_OPEN state
        """
        with self._lock:
            if self.state == self.STATE_OPEN:
                # Check if recovery timeout expired
                if self._should_attempt_recovery():
                    self.state = self.STATE_HALF_OPEN
                    logger.info(f"CircuitBreaker '{self.name}' transitioning to HALF_OPEN")
                else:
                    raise CircuitBreakerOpen(
                        f"CircuitBreaker '{self.name}' is OPEN. "
                        f"Service unavailable for another {self._recovery_remaining()}s"
                    )
        
        try:
            result = func(*args, **kwargs)
            
            # Success - reset circuit
            with self._lock:
                if self.state == self.STATE_HALF_OPEN:
                    logger.info(f"CircuitBreaker '{self.name}' recovered, transitioning to CLOSED")
                self.state = self.STATE_CLOSED
                self.failure_count = 0
                self.last_failure_time = None
            
            return result
        
        except self.expected_exception as e:
            with self._lock:
                self.failure_count += 1
                self.last_failure_time = threading.Event()  # Use timestamp
                self.last_failure_time = __import__('time').time()
                
                logger.warning(f"CircuitBreaker '{self.name}' failure {self.failure_count}/{self.failure_threshold}: {e}")
                
                if self.failure_count >= self.failure_threshold:
                    self.state = self.STATE_OPEN
                    logger.error(f"CircuitBreaker '{self.name}' OPENED after {self.failure_count} failures")
                elif self.state == self.STATE_HALF_OPEN:
                    # Recovery failed
                    self.state = self.STATE_OPEN
                    logger.error(f"CircuitBreaker '{self.name}' recovery failed, reopening")
            
            raise
    
    def _should_attempt_recovery(self) -> bool:
        """Check if enough time has passed to attempt recovery."""
        import time
        if not self.last_failure_time:
            return True
        elapsed = time.time() - self.last_failure_time
        return elapsed >= self.recovery_timeout
    
    def _recovery_remaining(self) -> int:
        """Calculate remaining recovery time in seconds."""
        import time
        if not self.last_failure_time:
            return 0
        elapsed = time.time() - self.last_failure_time
        remaining = int(self.recovery_timeout - elapsed)
        return max(0, remaining)
    
    def reset(self) -> None:
        """Manually reset circuit breaker."""
        with self._lock:
            self.state = self.STATE_CLOSED
            self.failure_count = 0
            self.last_failure_time = None
            logger.info(f"CircuitBreaker '{self.name}' manually reset")
    
    def get_state(self) -> str:
        """Get current circuit breaker state."""
        with self._lock:
            return self.state


class CircuitBreakerOpen(Exception):
    """Raised when circuit breaker is open (service unavailable)."""
    pass
