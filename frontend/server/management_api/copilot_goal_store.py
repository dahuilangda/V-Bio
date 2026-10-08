"""Session-goal persistence for the Copilot (goal retention, product-grade).

The goal object {goal_text, outline, plan_id, state} is written at
needs_input/await_confirmation terminals and cleared at complete/failed so
the ACTIVE GOAL survives the 6+12 message window (it used to leave the
context entirely after ~18 messages and the planner drifted).

Rows live in project_copilot_states: user_id + state_key =
`copilot-goal:{session_id}`, data jsonb. The table already exists
(zz_20260424_copilot_states.sql) with a unique (user_id, state_key) — upsert
is a merge-duplicates PATCH-style POST.
"""
from __future__ import annotations

from typing import Any, Dict

from management_api.postgrest_client import PostgrestClient


def goal_state_key(session_id: str) -> str:
    return f"copilot-goal:{str(session_id or '').strip()}"


class CopilotGoalStore:
    """Best-effort goal persistence over the shared PostgrestClient."""

    def __init__(self, postgrest: PostgrestClient) -> None:
        self.postgrest = postgrest

    def save(self, *, user_id: str, session_id: str, data: Dict[str, Any]) -> None:
        row = {
            "user_id": user_id,
            "state_key": goal_state_key(session_id),
            "data": data,
        }
        self.postgrest.request(
            "POST",
            "project_copilot_states",
            query={"on_conflict": "user_id,state_key"},
            payload=row,
            headers={"Prefer": "resolution=merge-duplicates"},
        )

    def clear(self, *, user_id: str, session_id: str) -> None:
        self.postgrest.request(
            "DELETE",
            "project_copilot_states",
            query={
                "user_id": f"eq.{user_id}",
                "state_key": f"eq.{goal_state_key(session_id)}",
            },
        )

    def get(self, *, user_id: str, session_id: str) -> Dict[str, Any] | None:
        rows = self.postgrest.request(
            "GET",
            "project_copilot_states",
            query={
                "user_id": f"eq.{user_id}",
                "state_key": f"eq.{goal_state_key(session_id)}",
                "select": "data",
                "limit": "1",
            },
        )
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            data = rows[0].get("data")
            return data if isinstance(data, dict) else None
        return None
