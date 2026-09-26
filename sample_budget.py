"""How many items of one service a SAMPLE run may still consider.

A quick migration copies a small section of each user's data -- the first N items
of each service -- so it finishes in a minute and can be checked one to one. Each
engine holds one Budget and asks it before it takes an item. Unlimited (the
ordinary case) it always says yes, so a full migration pays nothing for this.

The budget counts items CONSIDERED, not items that succeeded, so it is
deterministic: the same sample run twice looks at the same items, and the second
finds them already in the ledger.
"""
from __future__ import annotations

import threading


class Budget:
    def __init__(self, limit: int | None):
        self._left = limit
        self._lock = threading.Lock()      # Drive spends it from a pool of file workers

    @property
    def limited(self) -> bool:
        return self._left is not None

    @property
    def exhausted(self) -> bool:
        return self._left is not None and self._left <= 0

    def take(self) -> bool:
        """Spend one. False once the sample is used up."""
        if self._left is None:
            return True
        with self._lock:
            if self._left <= 0:
                return False
            self._left -= 1
            return True

    def trim(self, items: list) -> list:
        """The first items the budget still allows, spending them."""
        if self._left is None:
            return items
        with self._lock:
            keep = items[:max(0, self._left)]
            self._left -= len(keep)
            return keep
