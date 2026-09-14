"""Retrospective trace: turning an index of plate reads into a movement history.

This is the half of the system that answers "where has this vehicle been?" and
it is deliberately separate from live alerting (invariant 2). A trace
is a historical query an operator triggers; an alert is a streaming match on the
write path. Sharing code between them would eventually mean one of them silently
inherits the other's assumptions.
"""

from services.journey.reconstruct import (
    Hop,
    Journey,
    Leg,
    Visit,
    collapse_revisits,
    reconstruct,
)

__all__ = ["Hop", "Journey", "Leg", "Visit", "collapse_revisits", "reconstruct"]
