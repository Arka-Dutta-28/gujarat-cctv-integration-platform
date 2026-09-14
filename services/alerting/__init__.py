"""Live watchlist alerting — the second, separate code path (M5).

Invariant 2: retrospective trace and live alerting are
different things and must not share an implementation. Trace is a historical
query over `sightings`, triggered by an operator typing a plate. Alerting is a
streaming match evaluated on each new sighting as it is written. Both are
graded; building one and assuming it covers the other is the mistake the
build plan calls out by name.

This package holds only the alerting side: the match tiers, the watchlist the
matcher holds in memory, and the writer that turns a match into an alert.
"""
