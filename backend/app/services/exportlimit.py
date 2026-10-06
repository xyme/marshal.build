"""Light per-user limiter for zip-producing endpoints (pre-Beta hardening).

The model-call limiter (services/ratelimit.py) governs priced invocations;
export/bundle endpoints are unpriced but do real CPU/memory work per request
(zip assembly), so a beta user's retry loop or script shouldn't be able to
saturate a task. Fixed window, in-memory per task (two tasks → worst case 2×
the limit platform-wide — fine for the purpose), no configuration surface.
"""

import time
import uuid
from collections import defaultdict

from fastapi import HTTPException

EXPORTS_PER_MINUTE = 30

_windows: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))  # key -> (window, count)


def check_export_rate(user_id: uuid.UUID) -> None:
    """Raise 429 when the caller exceeds EXPORTS_PER_MINUTE this minute."""
    window = int(time.time() // 60)
    key = str(user_id)
    current_window, count = _windows[key]
    if current_window != window:
        _windows[key] = (window, 1)
        return
    if count >= EXPORTS_PER_MINUTE:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "export_rate_limited",
                "message": "Too many export downloads — try again in a minute.",
            },
            headers={"Retry-After": "60"},
        )
    _windows[key] = (window, count + 1)
