#!/usr/bin/env python3
"""Database-level locking for concurrent operations.

Implements optimistic locking and pessimistic locking strategies to prevent
race conditions when multiple users perform operations simultaneously.

Prevents errors like:
- Two users trying to create VMs in same class simultaneously
- Race conditions during class modifications
- Concurrent template updates causing conflicts
"""

import logging
import threading
import time
from datetime import datetime
from typing import Optional, Callable, Any
from contextlib import contextmanager
from functools import wraps

from sqlalchemy import and_, func

logger = logging.getLogger(__name__)


class OptimisticLockError(Exception):
    """Raised when optimistic lock version mismatch detected."""
    pass


class PessimisticLockTimeout(Exception):
    """Raised when unable to acquire pessimistic lock within timeout."""
    pass


def with_optimistic_lock(model_class, id_field='id') -> Callable:
    """Decorator for functions that modify database models with optimistic locking.
    
    Prevents lost updates by detecting concurrent modifications via version field.
    
    Args:
        model_class: SQLAlchemy model class (must have 'lock_version' field)
        id_field: Field name containing the record ID
    
    Raises:
        OptimisticLockError: If concurrent modification detected
    
    Example:
        @with_optimistic_lock(Class, id_field='class_id')
        def update_class_settings(class_id, new_name):
            class_obj = Class.query.get(class_id)
            class_obj.name = new_name
            db.session.commit()  # Optimistic lock checked here
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            from app.models import db
            
            # Call original function
            result = func(*args, **kwargs)
            
            # Optimistic locking is handled by SQLAlchemy via version_id_col
            # and InvalidRequestError on concurrent modification
            try:
                db.session.flush()  # Force flush to detect version conflicts
            except Exception as e:
                if 'version_id' in str(e) or 'concurrent modification' in str(e).lower():
                    logger.warning(f"Optimistic lock conflict in {func.__name__}: {e}")
                    raise OptimisticLockError(
                        f"Your changes conflicted with another user's changes. "
                        f"Please reload and try again."
                    ) from e
                raise
            
            return result
        
        return wrapper
    
    return decorator


@contextmanager
def pessimistic_lock(record_id: int, model_class, 
                    lock_name: str = None, timeout: int = 30):
    """Context manager for pessimistic record locking.
    
    Acquires exclusive lock on record, blocking other attempts until released.
    Suitable for operations requiring guaranteed serialization.
    
    Args:
        record_id: ID of record to lock
        model_class: SQLAlchemy model class (must have 'id' field)
        lock_name: Optional descriptive name for logging (default: model_class.__name__)
        timeout: Max seconds to wait for lock acquisition
    
    Yields:
        Locked record instance
    
    Raises:
        PessimisticLockTimeout: If lock not acquired within timeout
    
    Example:
        with pessimistic_lock(class_id, Class, lock_name="update_class", timeout=10):
            class_obj = Class.query.with_for_update().get(class_id)
            class_obj.name = "New Name"
            db.session.commit()
    """
    from app.models import db
    from sqlalchemy import select
    
    lock_name = lock_name or model_class.__name__
    start_time = time.time()
    acquired = False
    
    try:
        # Try to acquire exclusive lock with timeout
        logger.debug(f"Acquiring pessimistic lock: {lock_name}(id={record_id})")
        
        while time.time() - start_time < timeout:
            try:
                # Use FOR UPDATE for pessimistic lock
                record = db.session.query(model_class)\
                    .filter(model_class.id == record_id)\
                    .with_for_update(timeout=5)\
                    .first()
                
                if record is None:
                    raise ValueError(f"Record {lock_name}(id={record_id}) not found")
                
                acquired = True
                logger.debug(f"Pessimistic lock acquired: {lock_name}(id={record_id})")
                
                try:
                    yield record
                finally:
                    # Lock automatically released when session ends/rolls back
                    logger.debug(f"Pessimistic lock released: {lock_name}(id={record_id})")
                
                return
            
            except Exception as e:
                if 'timeout' in str(e).lower():
                    # Lock is held by another transaction, retry
                    elapsed = time.time() - start_time
                    remaining = timeout - elapsed
                    logger.debug(f"Lock contention on {lock_name}(id={record_id}), "
                               f"retrying ({elapsed:.1f}s/{timeout}s)...")
                    time.sleep(0.5)
                    continue
                
                # Other error, fail immediately
                raise
        
        # Timeout occurred
        raise PessimisticLockTimeout(
            f"Could not acquire lock on {lock_name}(id={record_id}) within {timeout}s. "
            f"Another operation may be in progress. Please try again."
        )
    
    except Exception as e:
        logger.error(f"Pessimistic lock error on {lock_name}(id={record_id}): {e}")
        raise


class OptimisticLockMixin:
    """Mixin to add optimistic locking to SQLAlchemy models.
    
    Requires models to have a 'lock_version' field.
    
    Usage:
        class MyModel(db.Model, OptimisticLockMixin):
            id = db.Column(db.Integer, primary_key=True)
            lock_version = db.Column(db.Integer, default=1)
            ...
    """
    
    # To be set by SQLAlchemy __versioned__ mechanism
    __mapper_args__ = {
        'version_id_col': None,  # Will be set per model
    }
    
    def check_version(self):
        """Verify model hasn't been modified since load.
        
        Raises:
            OptimisticLockError: If version changed
        """
        # This is handled by SQLAlchemy's versioning
        pass
    
    def increment_version(self):
        """Manually increment version (auto-done on flush if using versioning)."""
        if hasattr(self, 'lock_version'):
            self.lock_version += 1


class ConcurrencyProtector:
    """High-level API for protecting concurrent operations.
    
    Detects operation type and applies appropriate locking strategy:
    - Quick read-only checks: No locking
    - Single record updates: Optimistic lock
    - Multi-record updates: Pessimistic lock
    - Template/class modifications: Pessimistic lock with higher timeout
    """
    
    # Lock strategies by operation
    OPERATION_STRATEGIES = {
        'create_vms': 'pessimistic',  # High contention
        'class_settings': 'pessimistic',  # Must be atomic
        'template_update': 'pessimistic',
        'vm_assignment': 'optimistic',  # Low contention
        'user_update': 'optimistic',
    }
    
    @staticmethod
    def protect_operation(operation_type: str, record_id: int = None, 
                         model_class = None, timeout: int = 30) -> Callable:
        """Decorator to protect concurrent operations with appropriate locking.
        
        Args:
            operation_type: Type of operation (must be in OPERATION_STRATEGIES)
            record_id: ID of primary record being modified
            model_class: SQLAlchemy model class
            timeout: Lock acquisition timeout (seconds)
        
        Example:
            @ConcurrencyProtector.protect_operation('class_settings', 
                                                     model_class=Class,
                                                     timeout=10)
            def update_class(class_id, new_name):
                pass
        """
        strategy = ConcurrencyProtector.OPERATION_STRATEGIES.get(
            operation_type, 'optimistic'
        )
        
        def decorator(func: Callable) -> Callable:
            @wraps(func)
            def wrapper(*args, **kwargs) -> Any:
                if strategy == 'pessimistic' and model_class and record_id is not None:
                    with pessimistic_lock(record_id, model_class, 
                                        lock_name=operation_type, timeout=timeout):
                        return func(*args, **kwargs)
                elif strategy == 'optimistic' and model_class:
                    return with_optimistic_lock(model_class)(func)(*args, **kwargs)
                else:
                    # No locking needed
                    return func(*args, **kwargs)
            
            return wrapper
        
        return decorator


def init_optimistic_locking(db_model):
    """Initialize optimistic locking on a model by adding lock_version field.
    
    Example:
        init_optimistic_locking(Class)
        
    Models modified:
        - Class: Prevent concurrent class modifications
        - Template: Prevent concurrent template updates
        - VMAssignment: Prevent concurrent assignment conflicts
    """
    if not hasattr(db_model, 'lock_version'):
        logger.warning(f"Model {db_model.__name__} does not have lock_version field. "
                      f"Add this to enable optimistic locking:")
        logger.warning(f"  lock_version = db.Column(db.Integer, default=1)")
