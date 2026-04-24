"""Schema-backed Copilot skills.

Each module owns a small execution boundary: workflow detection, context-list
actions, or task-detail patches. The public compatibility layer re-exports
these helpers from management_api.copilot_capabilities.
"""

