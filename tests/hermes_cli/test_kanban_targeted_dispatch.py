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


def test_supervision_held_todo_is_not_promoted_or_globally_dispatched(
    board, all_assignees_spawnable
):
    token = "campaign-123:task-build"
    target = kb.create_task(
        board,
        title="held target",
        body=f"instructions\nHERMES_DISPATCH_HOLD={token}",
        assignee="alice",
        initial_status="todo",
    )
    spawned = []

    result = kbd.dispatch_once(
        board,
        spawn_fn=lambda task, workspace, **_kwargs: spawned.append(task.id) or 4242,
        max_spawn=1,
    )

    assert result.promoted == 0
    assert result.spawned == []
    assert spawned == []
    assert kb.get_task(board, target).status == "todo"


def test_targeted_dispatch_atomically_claims_matching_supervision_held_todo(
    board, all_assignees_spawnable
):
    token = "campaign-123:task-build"
    target = kb.create_task(
        board,
        title="held target",
        body=f"HERMES_DISPATCH_HOLD={token}",
        assignee="alice",
        initial_status="todo",
    )
    neighbor = kb.create_task(
        board,
        title="held neighbor",
        body="HERMES_DISPATCH_HOLD=campaign-123:task-review",
        assignee="alice",
        initial_status="todo",
    )

    refused = kbd.dispatch_task_once(
        board,
        target,
        hold_token="wrong-token",
        spawn_fn=lambda *_args, **_kwargs: pytest.fail("wrong hold token must not spawn"),
    )
    spawned = []
    result = kbd.dispatch_task_once(
        board,
        target,
        hold_token=token,
        spawn_fn=lambda task, workspace, **_kwargs: spawned.append(task.id) or 4242,
    )

    assert refused.spawned == []
    assert [row[0] for row in result.spawned] == [target]
    assert spawned == [target]
    assert kb.get_task(board, target).status == "running"
    assert kb.get_task(board, neighbor).status == "todo"


def test_targeted_dispatch_atomically_refuses_running_exclusive_sibling(
    board, all_assignees_spawnable
):
    token = "campaign-123:task-build"
    target = kb.create_task(
        board, title="held target", body=f"HERMES_DISPATCH_HOLD={token}",
        assignee="alice", initial_status="todo",
    )
    sibling = kb.create_task(board, title="running sibling", assignee="alice")
    assert kb.claim_task(board, sibling) is not None

    result = kbd.dispatch_task_once(
        board, target, hold_token=token, exclusive_task_ids=[sibling],
        spawn_fn=lambda *_args, **_kwargs: pytest.fail("exclusive sibling must prevent spawn"),
    )

    assert result.spawned == []
    assert kb.get_task(board, target).status == "todo"
    assert kb.list_runs(board, target) == []


@pytest.mark.parametrize("exclusive_task_ids", [
    ["t_missing"], "t_sibling", [None], [" t_sibling"], ["t_sibling", "t_sibling"],
])
def test_targeted_dispatch_fails_closed_for_invalid_exclusive_task_ids(
    board, all_assignees_spawnable, exclusive_task_ids
):
    token = "campaign-123:task-build"
    target = kb.create_task(
        board, title="held target", body=f"HERMES_DISPATCH_HOLD={token}",
        assignee="alice", initial_status="todo",
    )
    sibling = kb.create_task(board, title="running sibling", assignee="alice")
    assert kb.claim_task(board, sibling) is not None

    result = kbd.dispatch_task_once(
        board, target, hold_token=token, exclusive_task_ids=exclusive_task_ids,
        spawn_fn=lambda *_args, **_kwargs: pytest.fail("invalid exclusion must prevent spawn"),
    )

    assert result.spawned == []
    assert kb.get_task(board, target).status == "todo"
    assert kb.list_runs(board, target) == []


def test_dry_targeted_dispatch_of_held_todo_does_not_promote_or_open_run(
    board, all_assignees_spawnable
):
    token = "campaign-123:task-build"
    target = kb.create_task(
        board, title="held target", body=f"HERMES_DISPATCH_HOLD={token}",
        assignee="alice", initial_status="todo",
    )

    result = kbd.dispatch_task_once(board, target, hold_token=token, dry_run=True)

    assert [row[0] for row in result.spawned] == [target]
    assert kb.get_task(board, target).status == "todo"
    assert kb.list_runs(board, target) == []


def test_held_todo_refused_before_claim_remains_todo(board, monkeypatch):
    token = "campaign-123:task-build"
    target = kb.create_task(
        board, title="held target", body=f"HERMES_DISPATCH_HOLD={token}",
        assignee="missing-profile", initial_status="todo",
    )
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: False)

    result = kbd.dispatch_task_once(board, target, hold_token=token)

    assert result.skipped_nonspawnable == [target]
    assert kb.get_task(board, target).status == "todo"
    assert kb.list_runs(board, target) == []


def test_held_todo_dry_run_respects_unfinished_parent(board, all_assignees_spawnable):
    parent = kb.create_task(board, title="parent", assignee="alice")
    token = "campaign-123:task-build"
    target = kb.create_task(
        board, title="held child", body=f"HERMES_DISPATCH_HOLD={token}",
        assignee="alice", parents=[parent], initial_status="todo",
    )

    result = kbd.dispatch_task_once(board, target, hold_token=token, dry_run=True)

    assert result.spawned == []
    assert kb.get_task(board, target).status == "todo"
    assert kb.list_runs(board, target) == []


def test_indented_dispatch_hold_fails_closed_under_global_dispatch(
    board, all_assignees_spawnable
):
    target = kb.create_task(
        board, title="malformed held target", body=" HERMES_DISPATCH_HOLD=token",
        assignee="alice", initial_status="todo",
    )

    result = kbd.dispatch_once(board, max_spawn=1)

    assert result.promoted == 0
    assert result.spawned == []
    assert kb.get_task(board, target).status == "todo"


def test_held_review_requires_matching_token(board, all_assignees_spawnable):
    token = "campaign-123:task-review"
    target = kb.create_task(
        board, title="held review", body=f"HERMES_DISPATCH_HOLD={token}",
        assignee="alice", initial_status="todo",
    )
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (target,))

    assert kb.claim_review_task(board, target) is None
    result = kbd.dispatch_task_once(
        board, target, hold_token=token,
        spawn_fn=lambda *_args, **_kwargs: 4242,
    )

    assert [row[0] for row in result.spawned] == [target]
    assert kb.get_task(board, target).status == "running"


def test_malformed_dispatch_hold_cannot_be_authorized_with_empty_token(
    board, all_assignees_spawnable
):
    target = kb.create_task(
        board, title="malformed held target", body=" HERMES_DISPATCH_HOLD=token",
        assignee="alice", initial_status="todo",
    )
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (target,))

    result = kbd.dispatch_task_once(board, target, hold_token="")

    assert result.spawned == []
    assert kb.get_task(board, target).status == "ready"
    assert kb.list_runs(board, target) == []


def test_body_edit_cannot_remove_or_change_dispatch_hold(board):
    token = "campaign-123:task-build"
    target = kb.create_task(
        board, title="held target", body=f"instructions\nHERMES_DISPATCH_HOLD={token}",
        assignee="alice", initial_status="todo",
    )

    with pytest.raises(ValueError, match="dispatch hold is immutable"):
        kb.edit_task(board, target, body="instructions updated")
    with pytest.raises(ValueError, match="dispatch hold is immutable"):
        kb.edit_task(board, target, body="HERMES_DISPATCH_HOLD=other")

    assert kb.get_task(board, target).body.endswith(f"HERMES_DISPATCH_HOLD={token}")


def test_held_review_demoted_for_parent_resumes_in_review_lane(
    board, all_assignees_spawnable
):
    parent = kb.create_task(board, title="parent", assignee="alice")
    token = "campaign-123:task-review"
    target = kb.create_task(
        board, title="held review", body=f"HERMES_DISPATCH_HOLD={token}",
        assignee="reviewer", parents=[parent], initial_status="todo",
    )
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (target,))
    assert kb.claim_review_task(board, target, hold_token=token) is None
    assert kb.get_task(board, target).status == "todo"
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
    spawned = []

    result = kbd.dispatch_task_once(
        board, target, hold_token=token,
        spawn_fn=lambda task, *_args, **_kwargs: spawned.append(task) or 4242,
    )

    assert [row[0] for row in result.spawned] == [target]
    assert "sdlc-review" in spawned[0].skills
    claimed = [event for event in kb.list_events(board, target) if event.kind == "claimed"][-1]
    assert claimed.payload["source_status"] == "review"


def test_dispatch_cli_parses_and_routes_supervision_hold_token(board, monkeypatch):
    from hermes_cli import kanban as kb_cli

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    parsed = parser.parse_args([
        "kanban", "dispatch", "--task-id", "t_target",
        "--hold-token", "campaign-123:task-build",
        "--exclusive-task-id", "t_sibling", "--json",
    ])
    assert parsed.hold_token == "campaign-123:task-build"
    assert parsed.exclusive_task_ids == ["t_sibling"]

    captured = {}
    monkeypatch.setattr(
        kbd,
        "dispatch_task_once",
        lambda _conn, task_id, **kwargs: (
            captured.update(task_id=task_id, **kwargs), kb.DispatchResult()
        )[1],
    )
    args = argparse.Namespace(
        dry_run=True, max=None, failure_limit=2, json=False,
        task_id="t_target", hold_token="campaign-123:task-build",
        exclusive_task_ids=["t_sibling"], board=None,
    )
    assert kb_cli._cmd_dispatch(args) == 0
    assert captured["hold_token"] == "campaign-123:task-build"
    assert captured["exclusive_task_ids"] == ["t_sibling"]
