"""Usage limits for a public demo, so one visitor (or a bot) can't spend all the Gnani credits.

Two layers:
  - per visitor (IP address): how often they can clone, preview, start rooms and talk,
  - overall: a daily cap on Gnani API calls (GNANI_DAILY_LIMIT), enforced in GnaniClient.
"""
import time
from collections import defaultdict, deque

# action: (allowed calls, per this many seconds)
LIMITS = {
    "clone": (6, 3600),
    "preview": (60, 3600),
    "room": (30, 3600),
    "turn": (150, 3600),
}


class RateLimiter:
    def __init__(self, limits: dict[str, tuple[int, int]] = LIMITS):
        self.limits = limits
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)

    def allow(self, action: str, who: str) -> bool:
        limit, window = self.limits[action]
        hits = self._hits[(action, who)]
        now = time.monotonic()
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True
