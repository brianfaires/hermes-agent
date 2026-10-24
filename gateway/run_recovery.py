"""Recovery guidance for interrupted gateway turns."""
from __future__ import annotations


def build_resume_recovery_note(
    reason: str | None, message: str = "", *, interactive: bool = True,
) -> str:
    """Resume current authorized work after reconciling effects and newer instructions."""
    reason_phrase = (
        "a gateway restart" if reason == "restart_timeout"
        else "a gateway shutdown" if reason == "shutdown_timeout" else "a gateway interruption")
    if message:
        guidance = (
            "Address the user's NEW message below FIRST. A newer stop, cancellation, "
            "redirect or scope reduction takes priority over interrupted work. "
            "Continue earlier work only where it remains consistent with that instruction.")
    else:
        guidance = (
            "Review the current task, owner, approval reference, checkpoint and evidence; "
            "CONTINUE the interrupted task from its first unfinished authorized step. "
            "The interruption does not itself revoke approval or require another confirmation. "
            "Respect current holds, genuine expiry, permissions and ownership boundaries.")
        if not interactive:
            guidance += " No user is present; do not emit a restore-only acknowledgement or ask what next."
    return (
        f"[System note: The previous turn was interrupted by {reason_phrase}; "
        "the gateway is now back online. Any restart/shutdown command in the history "
        "has already run — do NOT re-execute or verify it. "
        f"{guidance} Tool results already appear in the history for completed steps; "
        "do not replay those mutations. For a missing result, inspect durable receipts "
        "and actual effects before retrying: a missing report does not prove the action failed. "
        "Perform only the next unfinished covered step, or report its concrete blocker "
        "to the responsible owner.]" + (f"\n\n{message}" if message else ""))
