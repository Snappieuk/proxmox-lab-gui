#!/usr/bin/env python3
"""
Caching utilities for the application.

This module provides thread-safe caching infrastructure used across the application.
"""

import threading
import time
import json
import os
import logging
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


class ThreadSafeCache:
    """A simple thread-safe cache with TTL support."""
    
    def __init__(self, ttl: int = 300):
        """Initialize cache with TTL in seconds."""
        self._data: Dict[str, Any] = {}
        self._timestamps: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._ttl = ttl
    
    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired."""
        with self._lock:
            if key in self._data:
                if time.time() - self._timestamps.get(key, 0) < self._ttl:
                    return self._data[key]
                else:
                    # Expired, remove
                    del self._data[key]
                    del self._timestamps[key]
        return None
    
    def set(self, key: str, value: Any) -> None:
        """Set value in cache."""
        with self._lock:
            self._data[key] = value
            self._timestamps[key] = time.time()
    
    def invalidate(self, key: Optional[str] = None) -> None:
        """Invalidate cache entry or entire cache."""
        with self._lock:
            if key is None:
                self._data.clear()
                self._timestamps.clear()
            elif key in self._data:
                del self._data[key]
                if key in self._timestamps:
                    del self._timestamps[key]
    
    def get_age(self, key: str) -> float:
        """Get age of cache entry in seconds, or -1 if not found."""
        with self._lock:
            if key in self._timestamps:
                return time.time() - self._timestamps[key]
        return -1


class PersistentCache:
    """Thread-safe cache that persists to a JSON file."""
    
    def __init__(self, file_path: str, ttl: int = 3600):
        """Initialize persistent cache.
        
        Args:
            file_path: Path to JSON file for persistence
            ttl: Time-to-live in seconds
        """
        self._file_path = file_path
        self._ttl = ttl
        self._data: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._loaded = False
    
    def _load(self) -> None:
        """Load cache from disk if not already loaded."""
        if self._loaded:
            return
        
        if not os.path.exists(self._file_path):
            self._data = {}
            self._loaded = True
            return
        
        try:
            with open(self._file_path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            logger.debug("Loaded cache from %s: %d entries", self._file_path, len(self._data))
        except Exception as e:
            logger.warning("Failed to load cache from %s: %s", self._file_path, e)
            self._data = {}
        
        self._loaded = True
    
    def _save(self) -> None:
        """Save cache to disk."""
        try:
            with open(self._file_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            logger.debug("Saved cache to %s: %d entries", self._file_path, len(self._data))
        except Exception as e:
            logger.warning("Failed to save cache to %s: %s", self._file_path, e)
    
    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired."""
        with self._lock:
            self._load()
            if key in self._data:
                entry = self._data[key]
                if time.time() - entry.get("timestamp", 0) < self._ttl:
                    return entry.get("value")
        return None
    
    def set(self, key: str, value: Any) -> None:
        """Set value in cache."""
        with self._lock:
            self._load()
            self._data[key] = {
                "value": value,
                "timestamp": time.time()
            }
            self._save()
    
    def invalidate(self, key: Optional[str] = None) -> None:
        """Invalidate cache entry or entire cache."""
        with self._lock:
            self._load()
            if key is None:
                self._data.clear()
            elif key in self._data:
                del self._data[key]
            self._save()
    
    def get_batch(self, keys: List[str]) -> Dict[str, Any]:
        """Get multiple values from cache efficiently."""
        results = {}
        now = time.time()
        
        with self._lock:
            self._load()
            for key in keys:
                if key in self._data:
                    entry = self._data[key]
                    if now - entry.get("timestamp", 0) < self._ttl:
                        results[key] = entry.get("value")
        
        return results
