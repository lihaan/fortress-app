# Fortress

Fortress is a small HTTP service that prevents a Windows PC from going to sleep on demand. It exposes a simple REST API so that other apps or scripts — running on the same machine or elsewhere on your network — can tell the PC to stay awake while they need it, then let it sleep again when they're done.

## Why does this exist?

Windows will put a PC to sleep after a period of inactivity. That's normally fine, but it becomes a problem if you're running background tasks (e.g. a long download, a sync job, a home server) and don't want to babysit the machine. Fortress lets any script or app remotely hold the PC awake for as long as needed, without any manual interaction.

## How it works

### The "lock" concept

The core idea is a **stay-awake lock**. Any caller that needs the PC awake sends a request to acquire a lock. Fortress keeps the PC awake as long as at least one lock is held. When a caller is done, it releases its lock. Once all locks are released, the PC is free to sleep again.

This design means multiple callers can independently hold the PC awake at the same time without needing to coordinate with each other — Fortress handles it.

Each lock:
- Gets a unique ID so callers can release exactly their own lock
- Can carry an optional name so you can tell which client holds it
- Automatically expires after a timeout (default: 60 minutes) as a safety net in case a caller crashes or forgets to release

### Staying awake on Windows

Fortress uses the Windows `SetThreadExecutionState` API to signal to the OS that the system is in use and should not sleep. This is the same mechanism apps like video players use to prevent sleep during playback. When all locks are released, Fortress clears that signal and Windows resumes normal power management.

### Persistence across restarts

Locks are saved to `active_locks.json` whenever they change. If Fortress restarts, it reloads any unexpired locks from that file and re-engages the stay-awake signal if needed. This means a service restart won't accidentally let the PC fall asleep mid-task.

### Background cleanup

Every 10 minutes, a background task checks for locks that have passed their expiry time and removes them. If that clears out all remaining locks, the stay-awake signal is released.

---

## File structure

```
fortress-app/
├── main.py              # Application entry point. Sets up the FastAPI server,
│                        # defines all HTTP endpoints, and manages the Windows
│                        # stay-awake signal.
│
├── lock.py              # Everything to do with locks: the data models, the registry
│                        # that stores active locks in memory, and the logic for
│                        # acquiring, releasing, expiring, saving, and loading them.
│
├── logging_config.py    # Configures logging so messages go to both the console
│                        # and app.log simultaneously.
│
├── active_locks.json    # Auto-generated file where active locks are persisted between
│                        # restarts. Created and managed by the app.
│
└── app.log              # Auto-generated log file written to as the app runs.
```

---

## Tech stack

- **[FastAPI](https://fastapi.tiangolo.com/)** — the web framework that handles HTTP requests
- **[Uvicorn](https://www.uvicorn.org/)** — the server that runs FastAPI
- **[Pydantic](https://docs.pydantic.dev/)** — validates the shape of incoming request data
- **Windows `SetThreadExecutionState` API** — the system call that actually prevents sleep

---

## API endpoints

| Method | Path | What it does |
|--------|------|--------------|
| `GET` | `/` | Health check — confirms the service is running |
| `GET` | `/status` | Returns current stay-awake state and number of active locks |
| `POST` | `/lock/acquire` | Acquires a stay-awake lock; returns a lock ID |
| `POST` | `/lock/release` | Releases a lock by ID |
| `GET` | `/lock/status` | Lists all active locks with their details |
| `POST` | `/keep-awake` | Directly engages stay-awake without a lock |
| `POST` | `/allow-sleep` | Directly releases stay-awake without a lock |

### Example: acquiring and releasing a lock

Acquire a lock (the PC will stay awake):

```bash
curl -X POST http://localhost:8000/lock/acquire \
  -H "Content-Type: application/json" \
  -d '{"client_name": "my-script", "timeout_minutes": 30}'
```

Response:
```json
{
  "success": true,
  "lock": {
    "id": "a1b2c3d4-...",
    "client_name": "my-script",
    "created_at": "2025-01-01T12:00:00",
    "expires_at": "2025-01-01T12:30:00"
  },
  "active_locks_count": 1,
  "awake_lock": true
}
```

Save the `id` from the response. When your task is done, release the lock:

```bash
curl -X POST http://localhost:8000/lock/release \
  -H "Content-Type: application/json" \
  -d '{"lock_id": "a1b2c3d4-..."}'
```

If no other locks are held, the PC will be free to sleep again.
