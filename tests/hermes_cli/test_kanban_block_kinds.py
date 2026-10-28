"""Tests for typed block reasons + the unblock-loop breaker.

Covers the built-in fix for the kanban "blocked loop" — a worker blocks a
task, a cron unblocks it, the worker re-blocks for the same reason, repeat
forever. The fix gives ``block_task`` a typed ``kind`` and a persistent
``block_recurrences`` counter:

* ``dependency`` blocks route to ``todo`` (parent-gated, auto-resumed) and
  never enter the human ``blocked`` bucket a cron would keep unblocking —
  unless no parent is open, in which case the wait can never be satisfied
  and the block is recorded as ``needs_input`` (sticky, loop-counted).
* ``needs_input`` / ``capability`` / un-typed blocks land in ``blocked``;
  each same-identity re-block after an unblock increments ``block_recurrences``,
  and at ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
* ``unblock_task`` deliberately does NOT reset ``block_recurrences`` (the
  amnesia that let the loop run unbounded).
* A successful ``complete_task`` resets the loop memory.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t", parents=()):
    """Create a task (linked under ``parents`` first) and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    for parent in parents:
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


# ---------------------------------------------------------------------------
# Loop breaker
# ---------------------------------------------------------------------------


def test_same_kind_different_derived_cause_resets_recurrence(kanban_home: Path) -> None:
    """Broad block kinds do not make unrelated blockers look recurrent."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="Need the finance owner to approve", kind="needs_input")
        first = kb.get_task(conn, tid)
        assert first is not None
        assert first.block_recurrences == 1
        first_fingerprint = first.blocker_fingerprint

        assert kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        assert kb.block_task(conn, tid, reason="Need the UX owner to choose a layout", kind="needs_input")

        second = kb.get_task(conn, tid)
        assert second is not None
        assert second.status == "blocked"
        assert second.block_recurrences == 1
        assert second.blocker_fingerprint != first_fingerprint
        payload = [e.payload for e in kb.list_events(conn, tid) if e.kind == "blocked"][-1]
        assert payload is not None
        assert payload["previous_blocker_fingerprint"] == first_fingerprint
        assert payload["blocker_fingerprint"] == second.blocker_fingerprint
        assert payload["previous_precondition_digest"] is None
        assert payload["precondition_digest"] is None
        assert payload["effective_limit"] == kb.BLOCK_RECURRENCE_LIMIT


def test_explicit_fingerprint_recurs_across_display_prose(kanban_home: Path) -> None:
    """Callers can keep stable blocker identity while improving human prose."""
    identity = "approval:security-owner"
    precondition = "repository-policy-v1"
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(
            conn, tid, reason="Security owner approval is required", kind="needs_input",
            blocker_fingerprint=identity, precondition_digest=precondition,
        )
        first = kb.get_task(conn, tid)
        assert first is not None
        assert first.blocker_fingerprint is not None
        assert first.precondition_digest is not None
        assert first.blocker_fingerprint.startswith("sha256:")
        assert first.precondition_digest.startswith("sha256:")
        assert identity not in first.blocker_fingerprint
        assert precondition not in first.precondition_digest

        assert kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        assert kb.block_task(
            conn, tid, reason="Still waiting for the designated security approver", kind="needs_input",
            blocker_fingerprint=identity, precondition_digest=precondition,
        )

        second = kb.get_task(conn, tid)
        assert second is not None
        assert second.status == "triage"
        assert second.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
        payload = [e.payload for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected"][-1]
        assert payload is not None
        assert payload["previous_blocker_fingerprint"] == payload["blocker_fingerprint"]
        assert payload["previous_precondition_digest"] == payload["precondition_digest"]
        assert payload["effective_limit"] == kb.BLOCK_RECURRENCE_LIMIT
        assert identity not in json.dumps(payload)
        assert precondition not in json.dumps(payload)


def test_changed_precondition_resets_same_explicit_blocker(kanban_home: Path) -> None:
    """The same blocker is fresh work when its relevant preconditions changed."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(
            conn, tid, reason="Run policy review", kind="needs_input",
            blocker_fingerprint="policy-review", precondition_digest="commit-a",
        )
        first = kb.get_task(conn, tid)
        assert first is not None

        assert kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        assert kb.block_task(
            conn, tid, reason="Run policy review again", kind="needs_input",
            blocker_fingerprint="policy-review", precondition_digest="commit-b",
        )

        second = kb.get_task(conn, tid)
        assert second is not None
        assert second.status == "blocked"
        assert second.block_recurrences == 1
        assert second.blocker_fingerprint == first.blocker_fingerprint
        assert second.precondition_digest != first.precondition_digest


def test_derived_identity_normalizes_reason_text(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="  Need   OWNER Approval  ", kind="needs_input")
        first = kb.get_task(conn, tid)
        assert first is not None

        assert kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        assert kb.block_task(conn, tid, reason="need owner approval", kind="needs_input")

        second = kb.get_task(conn, tid)
        assert second is not None
        assert second.blocker_fingerprint == first.blocker_fingerprint
        assert second.status == "triage"


def test_successful_completion_clears_block_identity(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(
            conn, tid, reason="Needs approval", kind="needs_input",
            blocker_fingerprint="approval", precondition_digest="revision-1",
        )
        assert kb.complete_task(conn, tid, result="resolved")
        completed = kb.get_task(conn, tid)
        assert completed is not None
        assert completed.status == "done"
        assert completed.block_kind is None
        assert completed.blocker_fingerprint is None
        assert completed.precondition_digest is None
        assert completed.block_recurrences == 0


def test_block_identity_columns_migrate_and_read_back(kanban_home: Path) -> None:
    """Legacy boards gain nullable identity columns without losing task reads."""
    with kbc.connect_closing() as conn:
        for column in ("blocker_fingerprint", "block_precondition_digest"):
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
            if column in columns:
                conn.execute(f"ALTER TABLE tasks DROP COLUMN {column}")

        kbc._migrate_add_optional_columns(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        assert {"blocker_fingerprint", "block_precondition_digest"} <= columns

        tid = kb.create_task(conn, title="legacy board task")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.blocker_fingerprint is None
        assert task.precondition_digest is None


def test_cli_block_threads_structured_identity(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    kanban_cli.build_parser(subparsers)
    args = parser.parse_args([
        "kanban", "block", tid, "Waiting", "for", "approval",
        "--kind", "needs_input",
        "--blocker-fingerprint", "approval:owner",
        "--precondition-digest", "revision:7",
    ])
    assert kanban_cli.kanban_command(args) == 0

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.blocker_fingerprint is not None
        assert task.precondition_digest is not None
        assert task.blocker_fingerprint.startswith("sha256:")
        assert task.precondition_digest.startswith("sha256:")










def test_block_loop_detected_event_emitted(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "block_loop_detected"]
        assert events, "expected a block_loop_detected event"
        payload = events[-1].payload or {}
        assert payload.get("recurrences") == 2
        assert payload.get("kind") == "capability"


# ---------------------------------------------------------------------------
# Dependency routing
# ---------------------------------------------------------------------------


def test_dependency_then_parent_done_promotes(kanban_home: Path) -> None:
    """A dependency-parked child becomes ready once its parent completes."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(
            conn,
            parent_id=parent,
            child_id=child,
            expected_child_run_id=kb.get_task(conn, child).current_run_id,
        )
        kb.block_task(conn, child, reason="wait", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        # Finish the parent, then let recompute_ready run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="worker")
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_dependency_block_with_terminal_parents_parks_then_escalates(
    kanban_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A ``dependency`` block whose parents are all terminal can never be
    satisfied by ``recompute_ready``: it must park in ``blocked`` as
    ``needs_input`` (no ``dependency_wait``, no re-promotion), say so on the
    CLI, and count toward the loop breaker so a re-block after an unblock
    reaches ``triage``."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="already-done-parent", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        # The edge exists before the run: link_tasks refuses to gate a running child retroactively.
        child = _running_task(conn, title="child-of-done", parents=(parent,))

        # `hermes kanban block <child> --kind dependency waiting on upstream`
        args = argparse.Namespace(
            task_id=child, ids=None, reason=["waiting", "on", "upstream"], kind="dependency",
            blocker_fingerprint="terminal-parent-wait", precondition_digest="parent-set-v1",
        )
        assert kanban_cli._cmd_block(args) == 0
        assert "needs_input" in capsys.readouterr().out
        parked = kb.get_task(conn, child)
        assert (parked.status, parked.block_kind, parked.block_recurrences) == ("blocked", "needs_input", 1)
        events = kb.list_events(conn, child)
        assert not [e for e in events if e.kind == "dependency_wait"]
        blocked = [e for e in events if e.kind == "blocked"][-1].payload
        assert (blocked["requested_kind"], blocked["rekind_reason"]) == ("dependency", "no_open_parent")
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "blocked"

        # A cron/human unblocks; the worker re-declares the same impossible wait.
        assert kb.unblock_task(conn, child)
        assert kb.claim_task(conn, child, claimer="worker") is not None
        assert kb.block_task(
            conn, child, reason="still waiting", kind="dependency",
            blocker_fingerprint="terminal-parent-wait", precondition_digest="parent-set-v1",
        )
        assert kb.get_task(conn, child).status == "triage"
        loop = [e for e in kb.list_events(conn, child) if e.kind == "block_loop_detected"][-1].payload
        assert loop["recurrences"] == kb.BLOCK_RECURRENCE_LIMIT


def test_dependency_block_with_open_parent_stays_parked_across_dispatch_tick(
    kanban_home: Path, all_assignees_spawnable,
) -> None:
    """Control: a genuine wait on an incomplete parent parks in ``todo``
    without counting a recurrence, survives a dispatch tick unspawned, and
    resumes once the parent finishes."""
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="open-parent", assignee="alice")
        # Linked while todo (a running child cannot be gated retroactively); the parent then stays
        # open while the child runs — the reopened-parent shape, forced the same way the loop below does.
        child = kb.create_task(conn, title="waiter", assignee="worker")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
        for _ in range(kb.BLOCK_RECURRENCE_LIMIT + 1):
            assert kb.block_task(conn, child, reason="wait", kind="dependency")
            parked = kb.get_task(conn, child)
            assert (parked.status, parked.block_kind, parked.block_recurrences) == ("todo", "dependency", 0)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
        assert kb.block_task(conn, child, reason="wait", kind="dependency")
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert kb.get_task(conn, child).status == "todo"
        assert child not in spawns
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="alice")
        kb.complete_task(conn, parent, result="done")
        res2 = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert child in [row[0] for row in res2.spawned]


# ---------------------------------------------------------------------------
# Completion resets loop memory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Validation + back-compat
# ---------------------------------------------------------------------------


def test_task_legacy_positional_constructor_keeps_established_field_bindings() -> None:
    """Appending task fields must not shift legacy positional callers."""
    legacy_prefix_fields = (
        "id", "title", "body", "assignee", "status", "priority", "created_by",
        "created_at", "started_at", "completed_at", "workspace_kind", "workspace_path",
        "claim_lock", "claim_expires", "tenant", "branch_name", "project_id", "result",
        "idempotency_key", "consecutive_failures", "worker_pid", "last_failure_error",
        "max_runtime_seconds", "last_heartbeat_at", "current_run_id", "workflow_template_id",
        "current_step_key", "skills", "model_override", "provider_override",
        "reasoning_effort", "max_retries", "goal_mode", "goal_max_turns", "session_id",
        "block_kind",
    )
    baseline = kb.Task(
        id="t_legacy", title="legacy", body=None, assignee=None, status="blocked",
        priority=0, created_by=None, created_at=None, started_at=None, completed_at=None,
        workspace_kind="scratch", workspace_path=None, claim_lock=None, claim_expires=None,
        tenant=None,
    )

    legacy_args = tuple(getattr(baseline, field) for field in legacy_prefix_fields)
    task_factory: Any = kb.Task
    task = task_factory(*(legacy_args + (2, "contract-v1")))

    assert task.block_recurrences == 2
    assert task.completion_contract == "contract-v1"
    assert task.blocker_fingerprint is None
    assert task.precondition_digest is None


