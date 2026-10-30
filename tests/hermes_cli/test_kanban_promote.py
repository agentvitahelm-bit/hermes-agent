"""Tests for the kanban `promote` verb (issue #28822).

The realistic bug scenario from #28822 is: a child task ends up in
``todo`` with all its parents already ``done`` (because the
auto-promote daemon hasn't run, or a manual close raced it).
Direct-SQL setup is used to construct that state deterministically.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _stuck_todo(conn, *, parents_done=True, n_parents=1):
    """Build the #28822 scenario: child in 'todo' whose parents may
    have closed as 'done' without the auto-promote logic firing.
    """
    parent_ids = [
        kb.create_task(conn, title=f"parent{i}", assignee="setup")
        for i in range(n_parents)
    ]
    child_id = kb.create_task(
        conn, title="child", parents=parent_ids, assignee="setup"
    )
    assert kb.get_task(conn, child_id).status == "todo"
    if parents_done:
        for pid in parent_ids:
            conn.execute(
                "UPDATE tasks SET status='done' WHERE id=?", (pid,)
            )
    return child_id, parent_ids


def test_promote_stuck_todo_succeeds(conn):
    child, _ = _stuck_todo(conn, parents_done=True)
    ok, err = kb.promote_task(conn, child, actor="tester")
    assert ok and err is None
    assert kb.get_task(conn, child).status == "ready"


def test_promote_refuses_undone_parent_and_names_the_real_remedy(conn):
    # #106195: promotion must never report a 'ready' that the first claim reverts.
    child, (parent,) = _stuck_todo(conn, parents_done=False)
    ok, err = kb.promote_task(conn, child, actor="tester", reason="recovery")
    assert not ok
    assert err and parent in err
    assert kb.get_task(conn, child).status == "todo"
    assert kb.claim_task(conn, child) is None  # still gated; nothing pretended




# ---------------------------------------------------------------------------
# CLI `_cmd_promote` — bulk via `--ids` (the issue's anti-respawn use case:
# promote all children of a closed parent in one command).
# ---------------------------------------------------------------------------


def _promote_ns(task_id, *, ids=None, reason=None, resolve_triage=False,
                dry_run=False, as_json=False):
    return argparse.Namespace(
        task_id=task_id,
        reason=list(reason or []),
        ids=list(ids or []) or None,
        resolve_triage=resolve_triage,
        dry_run=dry_run,
        json=as_json,
    )


def test_cli_promote_bulk_ids_promotes_all(kanban_home, capsys):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        children = [
            kb.create_task(conn, title=f"c{i}", parents=[parent])
            for i in range(3)
        ]
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    rc = kb_cli._cmd_promote(_promote_ns(children[0], ids=children[1:]))
    assert rc == 0
    out = capsys.readouterr().out
    for c in children:
        assert c in out
    with kbc.connect() as conn:
        for c in children:
            assert kb.get_task(conn, c).status == "ready"


def _loop_breaker_triage(conn):
    """Drive a real worker block/unblock/re-block episode into Triage."""
    tid = kb.create_task(conn, title="retry exact validation", assignee="tester")
    assert kb.claim_task(conn, tid, claimer="tester") is not None
    assert kb.block_task(conn, tid, reason="same failure", kind="transient")
    assert kb.unblock_task(conn, tid)
    assert kb.claim_task(conn, tid, claimer="tester") is not None
    assert kb.block_task(conn, tid, reason="same failure", kind="transient")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_promote_triage_rejects_ordinary_and_forged_callers(conn):
    tid = _loop_breaker_triage(conn)

    ok, err = kb.promote_task(conn, tid, actor="tester", reason="fresh evidence")
    assert not ok
    assert "--resolve-triage" in (err or "")

    ok, err = kb.promote_task(
        conn,
        tid,
        actor="tester",
        reason="fresh evidence",
        resolve_triage=True,
        _triage_capability=object(),
    )
    assert not ok
    assert "operator CLI" in (err or "")
    assert kb.get_task(conn, tid).status == "triage"


def test_promote_triage_requires_fresh_reason_and_current_loop_episode(conn):
    tid = _loop_breaker_triage(conn)

    ok, err = kb.promote_task(
        conn,
        tid,
        actor="tester",
        reason="  ",
        resolve_triage=True,
        _triage_capability=kb._TRIAGE_PROMOTION_CAPABILITY,
    )
    assert not ok
    assert "fresh audit reason" in (err or "")

    initial = kb.create_task(conn, title="ordinary triage", triage=True)
    ok, err = kb.promote_task(
        conn,
        initial,
        actor="tester",
        reason="not a loop breaker",
        resolve_triage=True,
        _triage_capability=kb._TRIAGE_PROMOTION_CAPABILITY,
    )
    assert not ok
    assert "current loop-breaker" in (err or "")


@pytest.mark.parametrize("superseding_kind", ["specified", "decomposed", "status", "imported"])
def test_promote_triage_rejects_superseded_loop_provenance(conn, superseding_kind):
    tid = _loop_breaker_triage(conn)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, superseding_kind, {"test": "new state generation"})

    ok, err = kb.promote_task(
        conn,
        tid,
        actor="tester",
        reason="historical loop evidence is stale",
        resolve_triage=True,
        _triage_capability=kb._TRIAGE_PROMOTION_CAPABILITY,
    )
    assert not ok
    assert "current loop-breaker" in (err or "")
    assert kb.get_task(conn, tid).status == "triage"


def test_promote_triage_rejects_unsatisfied_parent_and_live_run(conn):
    parent = kb.create_task(conn, title="unfinished parent")
    tid = _loop_breaker_triage(conn)
    kb.link_tasks(conn, parent, tid)

    ok, err = kb.promote_task(
        conn,
        tid,
        actor="tester",
        reason="parent still open",
        resolve_triage=True,
        _triage_capability=kb._TRIAGE_PROMOTION_CAPABILITY,
    )
    assert not ok
    assert parent in (err or "")

    with kb.write_txn(conn):
        conn.execute("DELETE FROM task_links WHERE parent_id=? AND child_id=?", (parent, tid))
        latest_run = kb.list_runs(conn, tid)[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (latest_run.id, tid))
    ok, err = kb.promote_task(
        conn,
        tid,
        actor="tester",
        reason="run is still bound",
        resolve_triage=True,
        _triage_capability=kb._TRIAGE_PROMOTION_CAPABILITY,
    )
    assert not ok
    assert "current loop-breaker" in (err or "")


def test_promote_triage_refuses_aba_replacement_episode(conn, monkeypatch):
    tid = _loop_breaker_triage(conn)
    original_txn = kb.write_txn

    @contextmanager
    def replace_episode_before_lock(target_conn):
        # Model a new loop-breaker generation arriving after validation but
        # before the promotion transaction acquires its write lock.
        with original_txn(target_conn):
            kb._append_event(target_conn, tid, "block_loop_detected", {"new_episode": True})
        with original_txn(target_conn):
            yield

    monkeypatch.setattr(kb, "write_txn", replace_episode_before_lock)
    ok, err = kb.promote_task(
        conn,
        tid,
        actor="tester",
        reason="decision for the prior episode",
        resolve_triage=True,
        _triage_capability=kb._TRIAGE_PROMOTION_CAPABILITY,
    )
    assert not ok
    assert "episode changed" in (err or "")
    assert kb.get_task(conn, tid).status == "triage"
    assert not [event for event in kb.list_events(conn, tid) if event.kind == "triage_resolved"]


def test_cli_resolve_triage_dry_run_then_resumes_same_card_without_completing(kanban_home, capsys):
    with kbc.connect() as conn:
        tid = _loop_breaker_triage(conn)
        child = kb.create_task(conn, title="downstream", parents=[tid])
        old_runs = [run.id for run in kb.list_runs(conn, tid)]
        before = kb.get_task(conn, tid)
        before_events = [(event.id, event.kind, event.payload) for event in kb.list_events(conn, tid)]

    reason = ["review", "PASS", "for", "exact", "SHA"]
    dry_rc = kb_cli._cmd_promote(
        _promote_ns(tid, reason=reason, resolve_triage=True, dry_run=True, as_json=True)
    )
    assert dry_rc == 0
    assert '"promoted": true' in capsys.readouterr().out
    with kbc.connect() as conn:
        after_dry_run = kb.get_task(conn, tid)
        assert after_dry_run.status == "triage"
        assert after_dry_run.completed_at == before.completed_at
        assert after_dry_run.block_kind == before.block_kind
        assert after_dry_run.block_recurrences == before.block_recurrences
        assert [run.id for run in kb.list_runs(conn, tid)] == old_runs
        assert [(event.id, event.kind, event.payload) for event in kb.list_events(conn, tid)] == before_events
        assert kb.get_task(conn, child).status == "todo"

    rc = kb_cli._cmd_promote(
        _promote_ns(tid, reason=reason, resolve_triage=True, as_json=True)
    )
    assert rc == 0
    assert '"promoted": true' in capsys.readouterr().out

    with kbc.connect() as conn:
        resumed = kb.get_task(conn, tid)
        assert resumed.status == "ready"
        assert resumed.completed_at is None
        assert resumed.block_kind is None
        assert resumed.block_recurrences == 0
        assert [run.id for run in kb.list_runs(conn, tid)] == old_runs
        assert kb.get_task(conn, child).status == "todo"
        events = kb.list_events(conn, tid)
        resolved = [event for event in events if event.kind == "triage_resolved"]
        assert len(resolved) == 1
        assert resolved[0].payload == {
            "resolution": "ready",
            "operator_override": True,
            "actor": "default",
            "reason": "review PASS for exact SHA",
        }
        assert kb.claim_task(conn, tid, claimer="tester") is not None
        assert kb.get_task(conn, tid).status == "running"


def test_cli_resolve_triage_refuses_bulk_scope(kanban_home, capsys):
    with kbc.connect() as conn:
        first = _loop_breaker_triage(conn)
        second = _loop_breaker_triage(conn)

    rc = kb_cli._cmd_promote(
        _promote_ns(
            first,
            ids=[second],
            reason=["review", "PASS"],
            resolve_triage=True,
        )
    )
    assert rc == 2
    assert "one exact task" in capsys.readouterr().err
    with kbc.connect() as conn:
        assert kb.get_task(conn, first).status == "triage"
        assert kb.get_task(conn, second).status == "triage"

