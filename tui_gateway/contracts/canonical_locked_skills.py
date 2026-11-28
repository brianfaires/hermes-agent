"""Closed native owner proposal wire; approving content is deliberately not an RPC."""
from typing import Literal

from .base import JsonValue, Params, Result
from .registry import canonical_method


class SkillProposalParams(Params):
    operation: Literal['prepare', 'freeze', 'show', 'apply', 'reconcile', 'enroll-start', 'enroll-finish']
    skill: str | None = None
    destination: str | None = None
    identifier: str | None = None
    pr_url: str | None = None
    digest: str | None = None
    attended_confirmation: bool | None = None


class SkillProposalResult(Result):
    result: dict[str, JsonValue]


canonical_method('skills.proposal', params=SkillProposalParams, result=SkillProposalResult,
                 doc='Native owner only: isolated locked-skill preparation, freeze, inspection, exact approved apply, '
                     'and separate Git reconciliation. Enrollment finalization requires attended verification. '
                     'No client approval/receipt/user-ID input; human approval is Telegram ingress only.')
