import pytest

from reelbot.db import DB
from reelbot.states import (
    RUNNING, STEP_FOR_QUEUED, TERMINAL, Event, InvalidTransition, JobState, can, next_state,
)

S, E = JobState, Event


def test_happy_path():
    s = S.COLLECTING
    for ev, expected in [
        (E.INSTRUCTION, S.PLAN_QUEUED), (E.START, S.PLANNING), (E.DONE, S.AWAITING_APPROVAL),
        (E.APPROVE, S.EXECUTE_QUEUED), (E.START, S.EXECUTING), (E.DONE, S.PREVIEW_READY),
        (E.FEEDBACK, S.REVISE_QUEUED), (E.START, S.REVISING), (E.DONE, S.PREVIEW_READY),
        (E.FINAL, S.FINAL_QUEUED), (E.START, S.FINALIZING), (E.DONE, S.DELIVERED),
    ]:
        s = next_state(s, ev)
        assert s == expected


def test_change_then_replan():
    s = next_state(S.AWAITING_APPROVAL, E.CHANGE)
    assert s == S.AWAITING_PLAN_FEEDBACK
    assert next_state(s, E.FEEDBACK) == S.PLAN_QUEUED
    # typing feedback straight away (without pressing Change) also re-plans
    assert next_state(S.AWAITING_APPROVAL, E.FEEDBACK) == S.PLAN_QUEUED


@pytest.mark.parametrize("running,fallback", [
    (S.PLANNING, S.FAILED), (S.EXECUTING, S.AWAITING_APPROVAL),
    (S.REVISING, S.PREVIEW_READY), (S.FINALIZING, S.PREVIEW_READY),
])
def test_failures_fall_back_to_actionable_state(running, fallback):
    assert next_state(running, E.FAIL) == fallback


@pytest.mark.parametrize("state", list(JobState))
def test_cancel_from_any_non_terminal(state):
    assert can(state, E.CANCEL) == (state not in TERMINAL)


@pytest.mark.parametrize("state,event", [
    (S.COLLECTING, E.APPROVE), (S.COLLECTING, E.FINAL), (S.PLANNING, E.APPROVE),
    (S.AWAITING_APPROVAL, E.FINAL), (S.DELIVERED, E.FEEDBACK), (S.EXECUTING, E.FEEDBACK),
])
def test_invalid_transitions(state, event):
    with pytest.raises(InvalidTransition):
        next_state(state, event)


def test_every_queued_state_starts_a_running_state():
    assert {r for r, _ in STEP_FOR_QUEUED.values()} == set(RUNNING)


def test_db_transitions_and_active_job():
    db = DB(":memory:")
    job = db.create_job(user_id=1, chat_id=1)
    assert db.active_job(1).id == job.id
    db.add_clip(job.id, "/x/clip01.mp4")
    job = db.transition(job.id, E.INSTRUCTION, instruction="punchy reel")
    assert job.state == S.PLAN_QUEUED and job.instruction == "punchy reel"
    assert job.clips == ["/x/clip01.mp4"]
    with pytest.raises(InvalidTransition):
        db.transition(job.id, E.FINAL)
    for ev in (E.START, E.DONE, E.APPROVE, E.START, E.DONE, E.FINAL, E.START, E.DONE):
        job = db.transition(job.id, ev)
    assert job.state == S.DELIVERED and job.delivered_at
    assert db.active_job(1) is None
    assert db.jobs_to_clean(older_than_s=0) and not db.jobs_to_clean(older_than_s=3600)


def test_metrics_roundtrip():
    db = DB(":memory:")
    db.log_metric("j1", "plan", "claude", True, 12.5, turns=7, input_tokens=100,
                  output_tokens=50, cost_usd=0.12)
    (m,) = db.metrics_for("j1")
    assert m["turns"] == 7 and m["cost_usd"] == 0.12 and m["ok"] == 1
