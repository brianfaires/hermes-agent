from __future__ import annotations


class PolicyError(PermissionError):
    pass


class CalendarStateError(PolicyError):
    pass
