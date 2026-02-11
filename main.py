import asyncio
import ctypes
import subprocess
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
import uvicorn

import logging_config
from lock import (
    LockRegistry,
    AcquireLockRequest,
    ReleaseLockRequest,
    lock_to_dict,
)

# Initialize logger
logger = logging.getLogger(__name__)

# Windows API Constants
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

# Configuration Constants
DEFAULT_TIMEOUT_MINUTES = 60
CLEANUP_INTERVAL_MINUTES = 10
LOCKS_FILE = Path(__file__).parent / "active_locks.json"

# Track current awake state
_is_awake = False

# Background cleanup task reference
_cleanup_task: Optional[asyncio.Task] = None


def set_awake_state(keep_awake: bool) -> bool:
    """
    Set the system's stay-awake state using Windows SetThreadExecutionState API.

    Args:
        keep_awake: If True, prevents the system from sleeping.
                    If False, returns to normal power management.

    Returns:
        the current awake state.
    """
    global _is_awake

    if keep_awake:
        # Prevent sleep: ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        )
        _is_awake = True
        logger.info("System stay-awake engaged")
    else:
        # Return to normal power management: ES_CONTINUOUS only
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        _is_awake = False
        logger.info("System stay-awake released")
    return _is_awake


def trigger_sleep() -> str:
    """
    Trigger an immediate system sleep using PowerShell.

    Uses the .NET System.Windows.Forms.Application.SetSuspendState method
    which reliably puts the system to sleep (not hibernate).

    Returns:
        A status message indicating the action taken.
    """
    command = [
        "powershell",
        "-Command",
        "Add-Type -AssemblyName System.Windows.Forms; "
        "[System.Windows.Forms.Application]::SetSuspendState("
        "[System.Windows.Forms.PowerState]::Suspend, $false, $false)",
    ]
    logger.info("Issuing system sleep command via PowerShell")
    subprocess.Popen(command, shell=False)
    return "Sleep command issued."


# Initialize lock registry with callbacks
lock_registry = LockRegistry(
    persistence_path=LOCKS_FILE,
    default_timeout_minutes=DEFAULT_TIMEOUT_MINUTES,
)


async def periodic_cleanup():
    """Background task that periodically cleans up expired locks."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_MINUTES * 60)
            removed = await lock_registry.cleanup_expired()
            if removed:
                logger.info(f"Periodic cleanup removed {removed} expired lock(s)")
            if lock_registry.count == 0:
                set_awake_state(False)
            else:
                # We do not try to engage stay-awake here as it should already be engaged
                # there might be instances where awake state is forcefully released, so the periodic cleanup should not interfere
                pass
        except asyncio.CancelledError:
            logger.info("Periodic cleanup task cancelled")
            break
        except Exception as e:
            logger.error(f"Error in periodic cleanup: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage application lifespan: load non-expired locks on startup, persist locks on shutdown.
    """
    global _cleanup_task
    
    # Startup: Load persisted locks
    loaded_count = lock_registry.load_from_file()
    
    # Engage stay-awake if there are active locks
    if lock_registry.count > 0:
        set_awake_state(True)
        logger.info(f"Fortress started: {loaded_count} active locks restored, stay-awake engaged")
    else:
        logger.info("Fortress started: No active locks, stay-awake not engaged")
    
    # Start periodic cleanup task
    _cleanup_task = asyncio.create_task(periodic_cleanup())
    logger.info(f"Periodic cleanup task started (interval: {CLEANUP_INTERVAL_MINUTES} minutes)")

    yield

    # Shutdown: Cancel cleanup task
    if _cleanup_task:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
    
    # Save current locks to file (prioritize persistence)
    lock_registry._save_to_file()
    logger.info(f"Fortress stopped: Saved {lock_registry.count} locks to persistent storage")

    # no need to explicitly release stay-awake here
    # since next startup event will cleanup expired locks as it loads from file

app = FastAPI(
    title="Fortress",
    description="Windows stay-awake service with remote control",
    lifespan=lifespan,
)


@app.get("/")
def root():
    """Health check endpoint."""
    logger.info("Health check endpoint hit")
    return {"service": "fortress", "status": "running"}


@app.get("/status")
def status():
    """Get the current stay-awake status."""
    logger.info(f"Status check - awake_lock: {_is_awake}, active_locks: {lock_registry.count}")
    return {
        "service": "fortress",
        "status": "running",
        "awake_lock": _is_awake,
        "active_locks_count": lock_registry.count,
    }


# ============== Lock Management Endpoints ==============

@app.post("/lock/acquire")
async def lock_acquire(request: Optional[AcquireLockRequest] = None):
    """
    Acquire a stay-awake lock.
    
    Issues a unique lock ID to the caller. The system will remain awake
    as long as at least one lock is active. Locks automatically expire
    after the specified DEFAULT_TIMEOUT_MINUTES.
    
    Returns:
        Lock details including the issued ID and expiration time.
    """
    client_name = request.client_name if request else None
    timeout_minutes = request.timeout_minutes if request else None
    
    logger.info(f"Lock acquire requested (client: {client_name or 'anonymous'}, timeout: {timeout_minutes or DEFAULT_TIMEOUT_MINUTES}min)")
    
    lock = await lock_registry.acquire(client_name=client_name, timeout_minutes=timeout_minutes)
    set_awake_state(True)

    return {
        "success": True,
        "lock": lock_to_dict(lock),
        "active_locks_count": lock_registry.count,
        "awake_lock": _is_awake,
    }


@app.post("/lock/release")
async def lock_release(request: ReleaseLockRequest, background_tasks: BackgroundTasks):
    """
    Release a stay-awake lock.
    
    When the caller is done with its interactions, it should call this
    endpoint with the lock ID that was issued. The stay-awake state will
    only be released when all active locks have been released.
    
    Returns:
        Success/failure status and remaining lock count.
    """
    logger.info(f"Lock release requested for ID: {request.lock_id}")
    
    success = await lock_registry.release(request.lock_id)

    if lock_registry.count == 0:
        # Defer releasing stay-awake to avoid mid-response sleep
        background_tasks.add_task(set_awake_state, False)
    
    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"Lock ID '{request.lock_id}' not found or already released"
        )
    
    return {
        "success": True,
        "lock_id": request.lock_id,
        "active_locks_count": lock_registry.count,
        "awake_lock": _is_awake,
    }


@app.get("/lock/status")
async def lock_status():
    """
    Get the status of all active locks.
    
    Returns:
        List of all active locks with their details.
    """
    logger.info(f"Lock status requested - {lock_registry.count} active locks")
    
    # Use mutex-protected method to get consistent snapshot
    locks = await lock_registry.get_all_locks()
    
    return {
        "active_locks_count": len(locks),
        "awake_lock": _is_awake,
        "locks": [lock_to_dict(lock) for lock in locks],
    }

# ============== Stay-Awake (Admin) Control Endpoints ==============

@app.post("/keep-awake")
def keep_awake():
    """
    Engage the stay-awake lock.
    Prevents the system from sleeping due to idle timeout.
    """
    logger.info("Keep-awake requested")
    message = set_awake_state(True)
    return {"message": message, "awake_lock": _is_awake}


@app.post("/allow-sleep")
def allow_sleep(background_tasks: BackgroundTasks):
    """
    Release the stay-awake lock.

    The lock release is deferred until after the response is sent, preventing
    Windows from sleeping mid-response when the idle timer has already expired.

    Returns:
        Status message indicating the action taken.
    """
    logger.info("Allow-sleep requested")

    background_tasks.add_task(set_awake_state, False)
    return {
        "message": "Stay-awake will be released shortly. System will sleep based on idle timers.",
        "awake_lock": False,
    }


if __name__ == "__main__":
    logger.info("Starting FastAPI server...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
