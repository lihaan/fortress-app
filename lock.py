"""
Lock management module for Fortress stay-awake service.

Provides a thread-safe registry for tracking active stay-awake locks
with automatic expiration and JSON persistence.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)


# ============== Pydantic Models ==============

class LockInfo(BaseModel):
    """Information about an active stay-awake lock."""
    id: str
    client_name: Optional[str] = None
    created_at: datetime
    expires_at: datetime


class AcquireLockRequest(BaseModel):
    """Request body for acquiring a new lock."""
    client_name: Optional[str] = Field(None, description="Optional identifier for the client machine")
    timeout_minutes: Optional[int] = Field(None, description="Lock timeout in minutes", gt=0)


class ReleaseLockRequest(BaseModel):
    """Request body for releasing an existing lock."""
    lock_id: str = Field(..., description="The lock ID to release")


# ============== Serialization Helpers ==============

def lock_to_dict(lock: LockInfo) -> dict:
    """
    Convert a LockInfo to a JSON-serializable dictionary.
    
    Used for both API responses and file persistence.
    """
    return {
        "id": lock.id,
        "client_name": lock.client_name,
        "created_at": lock.created_at.isoformat(),
        "expires_at": lock.expires_at.isoformat(),
    }

# ============== Lock Registry ==============

class LockRegistry:
    """
    Thread-safe registry for managing stay-awake locks.
    
    Encapsulates all lock state and provides synchronized access methods.
    Uses an asyncio.Lock to protect concurrent access in async contexts.
    
    Args:
        persistence_path: Path to JSON file for persisting locks across restarts.
        default_timeout_minutes: Default lock expiration time in minutes.
    """
    
    def __init__(
        self,
        persistence_path: Path,
        default_timeout_minutes: int = 60,
    ):
        self._locks: dict[str, LockInfo] = {}
        self._mutex = asyncio.Lock()
        self._persistence_path = persistence_path
        self._default_timeout_minutes = default_timeout_minutes
    
    
    @property
    def count(self) -> int:
        """
        Get the current number of active locks.
        
        Note: This is a non-blocking read. In Python, reading len(dict)
        is atomic, so mutex is not required for simple count checks.
        """
        return len(self._locks)
    
    # ============== Synchronized Methods ==============
    
    async def acquire(
        self,
        client_name: Optional[str] = None,
        timeout_minutes: Optional[int] = None,
    ) -> LockInfo:
        """
        Acquire a new stay-awake lock.
        
        Args:
            client_name: Optional identifier for the client machine.
            timeout_minutes: Lock timeout in minutes (uses default if not specified).
        
        Returns:
            LockInfo with the issued lock details.
        """
        async with self._mutex:
            timeout = timeout_minutes or self._default_timeout_minutes
            now = datetime.now()
            
            lock = LockInfo(
                id=str(uuid4()),
                client_name=client_name,
                created_at=now,
                expires_at=now + timedelta(minutes=timeout),
            )
            
            self._locks[lock.id] = lock
            self._save_to_file()
            
            logger.info(
                f"Lock acquired: {lock.id} "
                f"(client: {client_name or 'anonymous'}, expires: {lock.expires_at})"
            )
            return lock
    
    async def release(self, lock_id: str) -> bool:
        """
        Release a stay-awake lock by ID.
        
        Args:
            lock_id: The lock ID to release.
        
        Returns:
            True if lock was found and released, False otherwise.
        """
        async with self._mutex:
            if lock_id not in self._locks:
                logger.warning(f"Attempted to release unknown lock: {lock_id}")
                return False
            
            lock = self._locks.pop(lock_id)
            logger.info(f"Lock released: {lock_id} (client: {lock.client_name or 'anonymous'})")
            
            self._save_to_file()
            return True
    
    async def cleanup_expired(self) -> int:
        """
        Remove all expired locks.
        
        Returns:
            Number of locks removed.
        """
        async with self._mutex:
            now = datetime.now()
            expired_ids = [
                lock_id for lock_id, lock in self._locks.items()
                if lock.expires_at <= now
            ]
            
            for lock_id in expired_ids:
                lock = self._locks.pop(lock_id)
                logger.info(
                    f"Lock expired and removed: {lock_id} "
                    f"(client: {lock.client_name or 'anonymous'})"
                )
            
            if expired_ids:
                self._save_to_file()
            
            return len(expired_ids)
    
    async def get_all_locks(self) -> list[LockInfo]:
        """
        Get a snapshot of all active locks.
        
        Returns a copy to prevent iteration issues from concurrent modifications.
        Uses mutex to ensure consistent snapshot.
        """
        async with self._mutex:
            return list(self._locks.values())
    
    # ============== Persistence Methods ==============
    
    def _save_to_file(self) -> None:
        """
        Persist current locks to JSON file.
        
        Note: This method is called within mutex-protected contexts,
        so it does not acquire the mutex itself to avoid deadlock.
        """
        try:
            locks_data = {
                lock_id: lock_to_dict(lock)
                for lock_id, lock in self._locks.items()
            }
            self._persistence_path.write_text(json.dumps(locks_data, indent=2))
            logger.debug(f"Saved {len(locks_data)} locks to {self._persistence_path}")
        except Exception as e:
            logger.error(f"Failed to save locks to file: {e}")
    
    def load_from_file(self) -> int:
        """
        Load active locks from JSON file, filtering out expired entries.
        
        This method should only be called during startup before the server
        accepts requests, so mutex protection is not required.
        
        Returns:
            Number of valid locks loaded.
        """
        if not self._persistence_path.exists():
            logger.info("No existing locks file found, starting fresh")
            return 0
        
        try:
            locks_data = json.loads(self._persistence_path.read_text())
            now = datetime.now()
            loaded_count = 0
            
            for lock_id, lock_dict in locks_data.items():
                expires_at = datetime.fromisoformat(lock_dict["expires_at"])
                if expires_at > now:
                    self._locks[lock_id] = LockInfo(
                        id=lock_dict["id"],
                        client_name=lock_dict.get("client_name"),
                        created_at=datetime.fromisoformat(lock_dict["created_at"]),
                        expires_at=expires_at,
                    )
                    loaded_count += 1
                else:
                    logger.debug(f"Skipping expired lock {lock_id}")
            
            logger.info(
                f"Loaded {loaded_count} active locks from file "
                f"(filtered {len(locks_data) - loaded_count} expired)"
            )
            return loaded_count
        except Exception as e:
            logger.error(f"Failed to load locks from file: {e}")
            return 0