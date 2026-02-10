import ctypes
import subprocess
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI
import uvicorn

import logging_config

# Initialize logger
logger = logging.getLogger(__name__)

# Windows API Constants
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

# Track current awake state
_is_awake = False


def set_awake_state(keep_awake: bool) -> str:
    """
    Set the system's stay-awake state using Windows SetThreadExecutionState API.

    Args:
        keep_awake: If True, prevents the system from sleeping.
                    If False, returns to normal power management.

    Returns:
        A status message indicating the action taken.
    """
    global _is_awake

    if keep_awake:
        # Prevent sleep: ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        )
        _is_awake = True
        logger.info("System stay-awake engaged")
        return "System stay-awake engaged."
    else:
        # Return to normal power management: ES_CONTINUOUS only
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        _is_awake = False
        logger.info("System stay-awake released")
        return "System stay-awake released."


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage application lifespan: engage stay-awake on startup, release on shutdown.
    """
    # Startup: Keep system awake
    set_awake_state(True)
    logger.info("Fortress started: System stay-awake engaged")

    yield

    # Shutdown: Release stay-awake
    set_awake_state(False)
    logger.info("Fortress stopped: System stay-awake released")


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
    logger.info(f"Status check - awake_lock: {_is_awake}")
    return {"service": "fortress", "status": "running", "awake_lock": _is_awake}


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
def allow_sleep(force_now: bool = False, background_tasks: BackgroundTasks = None):
    """
    Release the stay-awake lock.

    Args:
        force_now: If True, triggers an immediate system sleep after releasing the lock.
                   The sleep is deferred until after the response is sent to the client.

    Returns:
        Status message indicating the action taken.
    """
    logger.info(f"Allow-sleep requested, force_now: {force_now}")
    message = set_awake_state(False)

    if force_now:
        background_tasks.add_task(trigger_sleep)
        return {
            "message": f"{message} Sleep command will be issued shortly.",
            "awake_lock": _is_awake,
            "sleeping": True,
        }

    return {
        "message": f"{message} System will sleep based on idle timers.",
        "awake_lock": _is_awake,
        "sleeping": False,
    }


if __name__ == "__main__":
    logger.info("Starting FastAPI server...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
