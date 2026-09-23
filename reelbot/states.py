"""Job state machine.

    COLLECTING --instruction--> PLAN_QUEUED --start--> PLANNING --done--> AWAITING_APPROVAL
    AWAITING_APPROVAL --approve--> EXECUTE_QUEUED --start--> EXECUTING --done--> PREVIEW_READY
    AWAITING_APPROVAL --change--> AWAITING_PLAN_FEEDBACK --feedback--> PLAN_QUEUED (re-plan)
    PREVIEW_READY --feedback--> REVISE_QUEUED --start--> REVISING --done--> PREVIEW_READY
    PREVIEW_READY --final--> FINAL_QUEUED --start--> FINALIZING --done--> DELIVERED

A failed step falls back to the last state the user can act on (so they can
retry), except PLANNING which has nothing to fall back to. Any non-terminal
state can be cancelled.
"""

from __future__ import annotations

from enum import StrEnum


class JobState(StrEnum):
    COLLECTING = "collecting"
    PLAN_QUEUED = "plan_queued"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_PLAN_FEEDBACK = "awaiting_plan_feedback"
    EXECUTE_QUEUED = "execute_queued"
    EXECUTING = "executing"
    PREVIEW_READY = "preview_ready"
    REVISE_QUEUED = "revise_queued"
    REVISING = "revising"
    FINAL_QUEUED = "final_queued"
    FINALIZING = "finalizing"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Event(StrEnum):
    INSTRUCTION = "instruction"
    START = "start"
    DONE = "done"
    FAIL = "fail"
    APPROVE = "approve"
    CHANGE = "change"
    FEEDBACK = "feedback"
    FINAL = "final"
    CANCEL = "cancel"


S, E = JobState, Event

TERMINAL = frozenset({S.DELIVERED, S.FAILED, S.CANCELLED})
RUNNING = frozenset({S.PLANNING, S.EXECUTING, S.REVISING, S.FINALIZING})
QUEUED = frozenset({S.PLAN_QUEUED, S.EXECUTE_QUEUED, S.REVISE_QUEUED, S.FINAL_QUEUED})

# queued state -> (running state, pipeline step name)
STEP_FOR_QUEUED = {
    S.PLAN_QUEUED: (S.PLANNING, "plan"),
    S.EXECUTE_QUEUED: (S.EXECUTING, "execute"),
    S.REVISE_QUEUED: (S.REVISING, "revise"),
    S.FINAL_QUEUED: (S.FINALIZING, "final"),
}

TRANSITIONS: dict[tuple[JobState, Event], JobState] = {
    (S.COLLECTING, E.INSTRUCTION): S.PLAN_QUEUED,
    (S.PLANNING, E.DONE): S.AWAITING_APPROVAL,
    (S.PLANNING, E.FAIL): S.FAILED,
    (S.AWAITING_APPROVAL, E.APPROVE): S.EXECUTE_QUEUED,
    (S.AWAITING_APPROVAL, E.CHANGE): S.AWAITING_PLAN_FEEDBACK,
    (S.AWAITING_APPROVAL, E.FEEDBACK): S.PLAN_QUEUED,
    (S.AWAITING_PLAN_FEEDBACK, E.FEEDBACK): S.PLAN_QUEUED,
    (S.AWAITING_PLAN_FEEDBACK, E.APPROVE): S.EXECUTE_QUEUED,
    (S.EXECUTING, E.DONE): S.PREVIEW_READY,
    (S.EXECUTING, E.FAIL): S.AWAITING_APPROVAL,
    (S.PREVIEW_READY, E.FEEDBACK): S.REVISE_QUEUED,
    (S.PREVIEW_READY, E.FINAL): S.FINAL_QUEUED,
    (S.REVISING, E.DONE): S.PREVIEW_READY,
    (S.REVISING, E.FAIL): S.PREVIEW_READY,
    (S.FINALIZING, E.DONE): S.DELIVERED,
    (S.FINALIZING, E.FAIL): S.PREVIEW_READY,
}
for _q, (_r, _) in STEP_FOR_QUEUED.items():
    TRANSITIONS[(_q, E.START)] = _r
for _s in JobState:
    if _s not in TERMINAL:
        TRANSITIONS[(_s, E.CANCEL)] = S.CANCELLED


class InvalidTransition(Exception):
    def __init__(self, state: JobState, event: Event):
        super().__init__(f"cannot {event.value} while job is {state.value}")
        self.state, self.event = state, event


def next_state(state: JobState | str, event: Event | str) -> JobState:
    state, event = JobState(state), Event(event)
    try:
        return TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransition(state, event) from None


def can(state: JobState | str, event: Event | str) -> bool:
    return (JobState(state), Event(event)) in TRANSITIONS
