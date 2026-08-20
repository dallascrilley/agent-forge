"""Error types for agent-forge."""


class SpecError(Exception):
    """A spec failed validation. Carries every problem found, not just the first."""

    def __init__(self, problems):
        self.problems = list(problems)
        msg = "spec validation failed:\n" + "\n".join(
            f"  - {p}" for p in self.problems
        )
        super().__init__(msg)


class AdapterError(Exception):
    """An adapter could not generate output for a valid spec."""
