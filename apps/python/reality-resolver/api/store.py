"""Read-only access to the case files the server is willing to serve.

No database, no cache, no resolution state - a case is read from disk
each time it is asked for, which for a handful of small JSON files is
both simpler and always current.

The name allowlist is the point of this module. Globbing cases/*.json
would have been shorter and is what this deliberately does not do: that
directory is also where an operator keeps local, untracked fixtures
holding real phone numbers for live CLI testing, and a glob would put
those in an HTTP response the moment they appeared on disk. Exposure is
therefore opt-in by name, in code, the same fail-closed shape as
dispatcher.UnknownJurisdictionError and use_cases.UnknownUseCaseError.

It doubles as the path-traversal defence: `name` is compared against a
fixed tuple before it is ever used to build a path, so no request can
reach outside cases/ regardless of what it contains.
"""

from __future__ import annotations

from pathlib import Path

from evidence.model import Case, load_case

DEFAULT_CASES_DIR = Path(__file__).resolve().parent.parent / "cases"

# The shipped catalog. Adding a case means adding its name here as well
# as the JSON file - see README's "Adding a use case".
SERVED_CASE_NAMES: tuple[str, ...] = (
    "critical-service-escalation",
    "ghost-appointment",
)


class CaseNotFoundError(LookupError):
    """Raised for a name that is not served, whether because it does not
    exist or because it is not on the allowlist. The two are deliberately
    indistinguishable to a client: which local files exist is not
    something an HTTP response should reveal.
    """


class CaseStore:
    def __init__(
        self,
        cases_dir: Path | None = None,
        served_names: tuple[str, ...] = SERVED_CASE_NAMES,
    ) -> None:
        self._dir = Path(cases_dir) if cases_dir is not None else DEFAULT_CASES_DIR
        self._served = served_names

    def names(self) -> tuple[str, ...]:
        """Served names that actually have a file, in allowlist order."""
        return tuple(name for name in self._served if (self._dir / f"{name}.json").is_file())

    def get(self, name: str) -> Case:
        if name not in self._served:
            raise CaseNotFoundError(name)
        path = self._dir / f"{name}.json"
        if not path.is_file():
            raise CaseNotFoundError(name)
        return load_case(path)

    def all(self) -> tuple[Case, ...]:
        return tuple(self.get(name) for name in self.names())
