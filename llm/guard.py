"""I-01: No-Reflection Rule enforcement (Feature 43B)."""

_BANNED = [
    "reflect on your",
    "critique your",
    "reconsider your",
    "review your previous",
]


def assert_no_reflection(prompt: str) -> None:
    lower = prompt.lower()
    for phrase in _BANNED:
        if phrase in lower:
            raise ValueError(
                f"No-Reflection Rule violated: prompt contains '{phrase}'. "
                "The SOAR loop has no Reflect step — remove this phrase."
            )
