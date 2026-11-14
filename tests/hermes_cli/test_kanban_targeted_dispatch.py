"""Exact-task Kanban dispatch must not sweep or launch neighboring work."""

import argparse
import hashlib
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_parser import build_parser


def _hold_token() -> str:
    return hashlib.sha256(b"buildfloor-held-todo").hexdigest()


def _hold_body(token: str) -> str:
    return f"BuildFloor campaign\nHERMES_DISPATCH_HOLD={token}\n"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def test_targeted_dispatch_spawns_only_the_requested_ready_task(board, all_assignees_spawnable):
    older = kb.create_task(board, title="older", assignee="alice")
    target = kb.create_task(board, title="target", assignee="alice")
    spawned = []

    result = kbd.dispatch_task_once(
        board,
        target,
        spawn_fn=lambda task, workspace, **_kwargs: spawned.append((task.id, workspace)) or 4242,
    )

    assert [row[0] for row in result.spawned] == [target]
    assert [row[0] for row in spawned] == [target]
    assert kb.get_task(board, target).status == "running"
    assert kb.get_task(board, older).status == "ready"


def test_targeted_dispatch_does_not_run_board_maintenance(board, all_assignees_spawnable):
    target = kb.create_task(board, title="target", assignee="alice")
    unrelated = kb.create_task(board, title="unrelated", assignee="alice")
    with kb.write_txn(board):
        board.execute(
            "UPDATE tasks SET status='todo' WHERE id=?",
            (unrelated,),
        )

    result = kbd.dispatch_task_once(
        board,
        target,
        spawn_fn=lambda *_args, **_kwargs: 4242,
    )

    assert result.promoted == 0
    assert kb.get_task(board, unrelated).status == "todo"
    assert kb.get_task(board, target).status == "running"


def test_targeted_dispatch_refuses_a_non_ready_task_without_mutation(board, all_assignees_spawnable):
    target = kb.create_task(board, title="target", assignee="alice")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='blocked' WHERE id=?", (target,))

    result = kbd.dispatch_task_once(
        board,
        target,
        spawn_fn=lambda *_args, **_kwargs: pytest.fail("blocked task must not spawn"),
    )

    assert result.spawned == []
    assert kb.get_task(board, target).status == "blocked"


def test_generic_dispatch_keeps_held_root_in_todo(board, all_assignees_spawnable):
    token = _hold_token()
    target = kb.create_task(
        board,
        title="held target",
        body=_hold_body(token),
        assignee="alice",
        initial_status="todo",
    )

    result = kbd.dispatch_once(
        board,
        spawn_fn=lambda *_args, **_kwargs: pytest.fail("held todo must not auto-dispatch"),
    )

    assert result.promoted == 0
    assert result.spawned == []
    assert kb.get_task(board, target).status == "todo"


def test_generic_dispatch_fails_closed_for_malformed_or_blocked_holds(
    board, all_assignees_spawnable
):
    malformed = kb.create_task(
        board,
        title="malformed hold",
        body="HERMES_DISPATCH_HOLD=malformed\n",
        assignee="alice",
        initial_status="todo",
    )
    blocked = kb.create_task(
        board,
        title="blocked hold",
        body=_hold_body(_hold_token()),
        assignee="alice",
        initial_status="blocked",
    )

    result = kbd.dispatch_once(
        board,
        spawn_fn=lambda *_args, **_kwargs: pytest.fail("held cards must not auto-dispatch"),
    )

    assert result.promoted == 0
    assert kb.get_task(board, malformed).status == "todo"
    assert kb.get_task(board, blocked).status == "blocked"


def test_targeted_dispatch_claims_exact_held_todo_with_matching_token(board, all_assignees_spawnable):
    token = _hold_token()
    target = kb.create_task(
        board,
        title="held target",
        body=_hold_body(token),
        assignee="alice",
        initial_status="todo",
    )
    spawned = []

    result = kbd.dispatch_task_once(
        board,
        target,
        hold_token=token,
        spawn_fn=lambda task, workspace, **_kwargs: spawned.append((task.id, workspace)) or 4242,
    )

    assert [row[0] for row in result.spawned] == [target]
    assert [row[0] for row in spawned] == [target]
    assert kb.get_task(board, target).status == "running"


@pytest.mark.parametrize("hold_token", [None, "0" * 64, "not-a-token"])
def test_targeted_dispatch_refuses_held_todo_without_exact_token(
    board, all_assignees_spawnable, hold_token
):
    token = _hold_token()
    target = kb.create_task(
        board,
        title="held target",
        body=_hold_body(token),
        assignee="alice",
        initial_status="todo",
    )

    result = kbd.dispatch_task_once(
        board,
        target,
        hold_token=hold_token,
        spawn_fn=lambda *_args, **_kwargs: pytest.fail("unauthorized held todo must not spawn"),
    )

    assert result.spawned == []
    assert kb.get_task(board, target).status == "todo"


def test_wrong_hold_token_is_a_read_only_noop_even_in_dry_run(
    board, monkeypatch, all_assignees_spawnable
):
    target = kb.create_task(
        board,
        title="unassigned held target",
        body=_hold_body(_hold_token()),
        initial_status="todo",
    )
    events_before = len(kb.list_events(board, target))
    monkeypatch.setattr(kbd, "_resolve_default_assignee", lambda _configured: "alice")

    result = kbd.dispatch_task_once(
        board,
        target,
        hold_token="0" * 64,
        dry_run=True,
        spawn_fn=lambda *_args, **_kwargs: pytest.fail("wrong token must not spawn"),
    )

    persisted = kb.get_task(board, target)
    assert result.spawned == []
    assert persisted.status == "todo"
    assert persisted.assignee is None
    assert len(kb.list_events(board, target)) == events_before


def test_targeted_dispatch_capability_advertises_held_todo_v2():
    assert kbd.TARGETED_DISPATCH_CAPABILITY == "hermes.kanban.dispatch-task-once/v2"


def test_dispatch_cli_parses_an_exact_task_target():
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))

    args = parser.parse_args(["kanban", "dispatch", "--task-id", "t_target", "--json"])

    assert args.task_id == "t_target"


def test_dispatch_cli_parses_a_hold_token_for_an_exact_task():
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    token = _hold_token()

    args = parser.parse_args([
        "kanban", "dispatch", "--task-id", "t_target", "--hold-token", token, "--json",
    ])

    assert args.task_id == "t_target"
    assert args.hold_token == token


def test_dispatch_cli_routes_an_exact_target_without_global_dispatch(board, monkeypatch):
    from hermes_cli import kanban as kb_cli

    captured = {}
    monkeypatch.setattr(
        kbd,
        "dispatch_task_once",
        lambda _conn, task_id, **kwargs: (
            captured.update(task_id=task_id, **kwargs),
            kb.DispatchResult(),
        )[1],
    )
    monkeypatch.setattr(
        kbd,
        "dispatch_once",
        lambda *_args, **_kwargs: pytest.fail("global dispatch must not run"),
    )
    args = argparse.Namespace(
        dry_run=True,
        max=None,
        failure_limit=2,
        json=False,
        task_id="t_target",
        hold_token=None,
        board=None,
    )

    assert kb_cli._cmd_dispatch(args) == 0
    assert captured["task_id"] == "t_target"
    assert captured["dry_run"] is True
    assert captured["hold_token"] is None


@pytest.mark.parametrize("task_id", ["", " ", " t_target", "t_target "])
def test_dispatch_cli_rejects_invalid_exact_target_without_global_dispatch(board, monkeypatch, task_id):
    from hermes_cli import kanban as kb_cli

    monkeypatch.setattr(kbd, "dispatch_task_once", lambda *_a, **_k: pytest.fail("invalid target must not dispatch"))
    monkeypatch.setattr(kbd, "dispatch_once", lambda *_a, **_k: pytest.fail("invalid target must not widen to global dispatch"))
    args = argparse.Namespace(
        dry_run=True, max=None, failure_limit=2, json=False,
        task_id=task_id, hold_token=None, board=None,
    )

    with pytest.raises(ValueError, match="non-empty task ID"):
        kb_cli._cmd_dispatch(args)


def test_targeted_dispatch_reports_default_assignment(board, monkeypatch, all_assignees_spawnable):
    target = kb.create_task(board, title="target")
    monkeypatch.setattr(kbd, "_resolve_default_assignee", lambda _configured: "alice")

    result = kbd.dispatch_task_once(
        board,
        target,
        spawn_fn=lambda *_args, **_kwargs: 4242,
    )

    assert result.auto_assigned_default == [target]
    persisted = kb.get_task(board, target)
    assert persisted is not None and persisted.assignee == "alice"
    assert [row[0] for row in result.spawned] == [target]
