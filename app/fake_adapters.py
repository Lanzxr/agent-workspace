from __future__ import annotations

from .control_plane import ControlPlane
from .models import Event, WorkItem


class DeterministicAssistantAdapter:
    """A deterministic proposal adapter for control-plane tests only."""

    def __init__(self, control_plane: ControlPlane) -> None:
        self.control_plane = control_plane

    def propose_work(
        self,
        *,
        event: Event,
        work_item_id: str,
        title: str,
        objective: str,
        owner_ref: str | None = None,
    ) -> WorkItem:
        self.control_plane.ingest_event(event)
        self.control_plane.create_work_item(
            work_item_id=work_item_id,
            title=title,
            objective=objective,
            source_event_ids=[event.event_id],
            completion_conditions=["backend records a result"],
            risk_class="low",
            owner_ref=owner_ref,
        )
        return self.control_plane.propose_work_item(work_item_id)

