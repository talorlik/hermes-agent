from __future__ import annotations


def delta_allowed(
    path: str,
    kind: str,
    *,
    equals_upstream: bool,
    equals_fork: bool,
    pin_keys_only: bool,
    contract_ok: bool,
    executable_drop: bool,
) -> bool:
    """Return whether a sync delta of this file class may be applied.

    ``path`` is accepted so callers can name the changed file. The boolean
    follows ``kind``. An unknown kind is refused.
    """
    del path
    if kind == "generated-unspecified":
        return equals_upstream
    if kind == "generated-specified":
        return pin_keys_only
    if kind == "structured-pin":
        return pin_keys_only and contract_ok
    if kind == "auth-hold":
        return equals_fork
    if kind == "build-output":
        return False
    if kind == "owned-source":
        return not executable_drop
    return False
