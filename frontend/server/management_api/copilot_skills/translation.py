"""Translation as an atomic Copilot operation.

The reference databases are English-indexed, so a non-English entity name must be converted to
its English form before it is queried. The conversion itself uses the planner's own language
ability — no dedicated model call, no external service. ``translate.to_english`` simply makes
that conversion an explicit unit operation: the planner passes the name exactly as the user gave
it plus its own English rendering, the harness records both as an observation, and the follow-up
lookup consumes the recorded English form via ``$fromObservation``.
"""

from __future__ import annotations

from typing import Any, Dict

from management_api.copilot_skills.online_databases import OnlineSkillDefinition


def _translate_locally(arguments: Dict[str, Any]) -> Dict[str, Any]:
    text = str(arguments.get("text") or "").strip()
    english = str(arguments.get("english") or "").strip()
    if not text:
        raise ValueError("translate.to_english requires a non-empty text.")
    if not english:
        raise ValueError(
            "translate.to_english requires the English form in 'english' — you perform the "
            "conversion yourself and state it here, so it is recorded as a unit operation."
        )
    domain = str(arguments.get("domain") or "general").strip() or "general"
    return {
        "source": "translate",
        "query": text,
        "count": 1,
        "results": [{"original": text, "english": english, "domain": domain}],
    }


def register_translation_skills(skills: Any) -> None:
    """Register the translation operation onto a read-skill registry."""
    skills.register(
        OnlineSkillDefinition(
            name="translate.to_english",
            accepts_non_english_input=True,
            description=(
                "Translate one entity name (compound, protein, gene, organism, disease) into its "
                "standard English form, as one explicit unit operation. You perform the conversion "
                "yourself with your own language ability: pass the name exactly as the user gave it in "
                "'text' and your English rendering of it in 'english'. The databases this platform "
                "queries are English-indexed — when the user names an entity in another language, emit "
                "this operation first, then query with the recorded English form consumed via "
                "$fromObservation. A name that is already English needs no translation step. This "
                "operation neither adds information nor verifies the entity exists."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 512,
                        "description": "The entity name exactly as the user gave it.",
                    },
                    "english": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 512,
                        "description": "Your English rendering of the name (standard nomenclature for the domain).",
                    },
                    "domain": {
                        "type": "string",
                        "enum": ["compound", "protein", "organism", "disease", "general"],
                        "description": "The terminology domain of the name, for disambiguation.",
                    },
                },
                "required": ["text", "english"],
                "additionalProperties": False,
            },
        ),
        _translate_locally,
    )
