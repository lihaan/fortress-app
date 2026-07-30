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

    The execution state is per-thread and can only be withdrawn by the thread
    that requested it, so every caller must run on the event loop thread. See
    release_awake_state for how deferred releases keep that guarantee.

    Args:
        keep_awake: If True, prevents the system from sleeping.
                    If False, returns to normal power management.

    Returns:
        the current awake state (unchanged if the API call failed).
    """
    global _is_awake

    state = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if keep_awake else ES_CONTINUOUS

    # Returns the previous flags, or 0 on failure. Leave _is_awake untouched
    # on failure so it stays honest about the real system state.
    if ctypes.windll.kernel32.SetThreadExecutionState(state) == 0:
        logger.error("SetThreadExecutionState failed, stay-awake state unchanged")
        return _is_awake

    _is_awake = keep_awake
    logger.info(f"System stay-awake {'engaged' if keep_awake else 'released'}")
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


async def release_awake_state() -> None:
    """
    Release the stay-awake state from the event loop thread.

    This wrapper looks pointless but MUST stay a coroutine: Starlette awaits
    coroutine background tasks inline on the event loop, whereas it pushes a
    plain function to a worker thread. There the per-thread withdrawal would
    silently do nothing, because the event loop thread owns the request (locks
    are acquired from async endpoints).
    """
    set_awake_state(False)


async def release_awake_state_if_idle() -> None:
    """
    Release the stay-awake state, but only if no locks are left.

    The count MUST be re-checked here, at execution time: this runs after the
    response is sent, and a /lock/acquire landing in between must win.

    Nothing between the check and the withdrawal may actually suspend, or an
    acquire could slip in after the check and be overruled. Awaiting
    release_awake_state is safe because it never yields to the event loop.
    """
    if lock_registry.count == 0:
        await release_awake_state()


async def periodic_cleanup():
    """Background task that periodically cleans up expired locks."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_MINUTES * 60)
            removed = await lock_registry.cleanup_expired()
            if removed:
                logger.info(f"Periodic cleanup removed {removed} expired lock(s)")
            if lock_registry.count == 0 and _is_awake:
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

    if not success:
        # Raise before scheduling: FastAPI discards background tasks when the
        # handler raises, so anything queued above here would never run.
        raise HTTPException(
            status_code=404,
            detail=f"Lock ID '{request.lock_id}' not found or already released"
        )

    # Deferred to avoid sleeping mid-response. The task decides for itself
    # whether any locks are left, so there is nothing to check here.
    background_tasks.add_task(release_awake_state_if_idle)

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
async def keep_awake():
    """
    Engage the stay-awake lock.
    Prevents the system from sleeping due to idle timeout.

    Async so the request is made from the event loop thread. A sync endpoint
    runs on a worker thread that Windows discards once it goes idle, taking
    the stay-awake request with it.
    """
    logger.info("Keep-awake requested")
    message = set_awake_state(True)
    return {"message": message, "awake_lock": _is_awake}


@app.post("/allow-sleep")
async def allow_sleep(background_tasks: BackgroundTasks):
    """
    Release the stay-awake lock.

    The lock release is deferred until after the response is sent, preventing
    Windows from sleeping mid-response when the idle timer has already expired.

    Returns:
        Status message indicating the action taken.
    """
    logger.info("Allow-sleep requested")

    # Unconditional: this is the admin override, so it ignores the lock count.
    background_tasks.add_task(release_awake_state)
    return {
        "message": "Stay-awake will be released shortly. System will sleep based on idle timers.",
        "awake_lock": False,
    }


if __name__ == "__main__":
    logger.info("Starting FastAPI server...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
