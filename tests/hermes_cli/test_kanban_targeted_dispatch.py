"""Exact-task Kanban dispatch must not sweep or launch neighboring work."""

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_parser import build_parser


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


def test_dispatch_cli_parses_an_exact_task_target():
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))

    args = parser.parse_args(["kanban", "dispatch", "--task-id", "t_target", "--json"])

    assert args.task_id == "t_target"


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
        board=None,
    )

    assert kb_cli._cmd_dispatch(args) == 0
    assert captured["task_id"] == "t_target"
    assert captured["dry_run"] is True


@pytest.mark.parametrize("task_id", ["", " ", " t_target", "t_target "])
def test_dispatch_cli_rejects_invalid_exact_target_without_global_dispatch(board, monkeypatch, task_id):
    from hermes_cli import kanban as kb_cli

    monkeypatch.setattr(kbd, "dispatch_task_once", lambda *_a, **_k: pytest.fail("invalid target must not dispatch"))
    monkeypatch.setattr(kbd, "dispatch_once", lambda *_a, **_k: pytest.fail("invalid target must not widen to global dispatch"))
    args = argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False, task_id=task_id, board=None)

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
