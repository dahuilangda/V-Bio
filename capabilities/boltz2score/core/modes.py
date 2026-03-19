from __future__ import annotations

SCORE_MODE = "score"
POSE_MODE = "pose"
REFINE_MODE = "refine"
INTERFACE_MODE = "interface"

SUPPORTED_MODES = (
    SCORE_MODE,
    POSE_MODE,
    REFINE_MODE,
    INTERFACE_MODE,
)

MODE_DESCRIPTIONS = {
    SCORE_MODE: "confidence scoring only",
    POSE_MODE: "refine while keeping the input pose close",
    REFINE_MODE: "general flexible refinement",
    INTERFACE_MODE: "interface-focused flexible refinement",
}

OPTIMIZATION_MODE_DESCRIPTIONS = {
    POSE_MODE: MODE_DESCRIPTIONS[POSE_MODE],
    REFINE_MODE: MODE_DESCRIPTIONS[REFINE_MODE],
    INTERFACE_MODE: MODE_DESCRIPTIONS[INTERFACE_MODE],
}


def _supported_description(descriptions: dict[str, str]) -> str:
    return ", ".join(
        f"{name} ({description})"
        for name, description in descriptions.items()
    )

def normalize_mode_name(value: str, *, allow_score: bool = True) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in SUPPORTED_MODES:
        normalized = None
    if normalized == SCORE_MODE and not allow_score:
        normalized = None
    if normalized is None:
        descriptions = MODE_DESCRIPTIONS if allow_score else OPTIMIZATION_MODE_DESCRIPTIONS
        raise ValueError(
            f"Unsupported mode {value!r}. Supported modes: "
            f"{', '.join(descriptions)}."
        )
    return normalized


def mode_help_text(*, allow_score: bool = True) -> str:
    descriptions = MODE_DESCRIPTIONS if allow_score else OPTIMIZATION_MODE_DESCRIPTIONS
    return f"Pipeline mode. Supported modes: {_supported_description(descriptions)}."
