"""Regression check: is the stay-awake request released from the right thread?

SetThreadExecutionState is per-thread. The request belongs to the thread that
made it and can only be withdrawn by that same thread. Locks are acquired from
async endpoints, so the event loop thread owns it, and every release path must
run there too. A release handed to BackgroundTasks as a plain function runs on
a worker thread instead, where the withdrawal silently does nothing and the
machine never idle-sleeps again until this process restarts. That shipped once.

Nothing in the app's own logs or HTTP responses can catch that: _is_awake goes
False either way. So this asks the OS instead, from the event loop thread.

Run it after touching anything in the stay-awake path:

    app_env\\Scripts\\python.exe check_awake_thread.py

Exits 0 if the request is held while a lock exists and gone after release,
non-zero otherwise. Uses a temporary lock file, so active_locks.json is left
alone. Needs a free port on localhost; it starts a real server.
"""
import ctypes
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

PORT = 8127
ES_CONTINUOUS = 0x80000000
HELD = 0x80000001  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED


def run_phase(release: bool) -> bool:
    """Start the real app, drive it, then ask the OS what the loop thread holds."""
    import uvicorn

    import main

    kernel32 = ctypes.windll.kernel32
    kernel32.SetThreadExecutionState.restype = ctypes.c_uint
    kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint]

    @main.app.get("/debug/execution-state")
    async def execution_state():
        # Async, so this runs on the event loop thread: the one that owns the
        # request. Reading it also clears it, which is fine at end of phase.
        return {"previous": kernel32.SetThreadExecutionState(ES_CONTINUOUS)}

    # Keep the repo's real lock file out of it.
    main.lock_registry._persistence_path = pathlib.Path(tempfile.mkdtemp()) / "locks.json"

    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(2)

    try:
        lock = post("/lock/acquire", {"client_name": "check_awake_thread"})["lock"]
        if release:
            post("/lock/release", {"lock_id": lock["id"]})
            # The background task runs after the response is sent, then waits
            # out the delivery grace. Read the real constant so the two cannot
            # drift apart.
            time.sleep(main.SLEEP_GRACE_SECONDS + 0.6)

        held = get("/debug/execution-state")["previous"] == HELD
        expected = not release
        label = "after release" if release else "while a lock is held"
        print(f"  {label}: request held = {held} (expected {expected})")
        return held == expected
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def post(path, body):
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(request).read())


def get(path):
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}").read())


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Child: one phase. Each needs a fresh process, since checking the
        # state also clears it and the two phases would contaminate each other.
        sys.exit(0 if run_phase(release=sys.argv[1] == "release") else 1)

    ok = True
    for phase in ("hold", "release"):
        result = subprocess.run([sys.executable, __file__, phase], stderr=subprocess.DEVNULL)
        ok = ok and result.returncode == 0

    print("PASS" if ok else "FAIL: stay-awake is not being released from the event loop thread")
    sys.exit(0 if ok else 1)
