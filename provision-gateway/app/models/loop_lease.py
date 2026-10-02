"""Loop lease — makes a periodic background loop single-home (GRILLING G31).

The gateway runs `uvicorn --workers 4`, and each worker runs the lifespan, so
every background loop used to be started FOUR times: four reconcilers, four
pre-deploy reconcilers, four docker-events monitors, four project monitors.
Three of those are only waste; the pre-deploy one corrupted (see D2).

A lease row is the cheapest fix that needs no new process: a worker claims the
loop for a window longer than the loop's own interval, so it keeps winning;
the others skip. If the holder dies, the lease expires and the next worker to
try takes over — which is why this is a time-based lease and not a leader
election with its own liveness protocol.
"""

from __future__ import annotations

from sqlalchemy import Column, Float, String

from ..database import Base


class LoopLease(Base):
    __tablename__ = "loop_leases"

    name = Column(String(80), primary_key=True)
    locked_until = Column(Float, nullable=False, default=0.0)
