"""Managed Kanban workers retain the shared board authority across profile routing."""
import json
import os
from pathlib import Path

import pytest

from gateway import session_kanban
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing


def test_bound_routed_worker_receives_shared_kanban_home(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    profile_home = shared_home / "profiles" / "code-builder"
    workspace = tmp_path / "workspace"
    profile_home.mkdir(parents=True)
    workspace.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(shared_home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(shared_home))
    board = "shared-board"
    kb.create_board(board)
    path = kb.kanban_db_path(board=board).resolve()
    with connect_closing(path) as conn:
        task_id = kb.create_task(conn, title="shared task", assignee="code-builder")
        task = kb.claim_task(conn, task_id, claimer="private-host:owner")
        assert task is not None

    context = {
        "board": board,
        "db": str(path),
        "kanban_home": str(shared_home.resolve()),
        "task_id": task_id,
        "run_id": task.current_run_id,
        "claim_lock": task.claim_lock,
        "workspace": str(workspace),
        "workspaces_root": str(kb.workspaces_root(board=board).resolve()),
        "profile": "code-builder",
        "branch": None,
        "tenant": None,
        "goal_mode": False,
        "max_runtime_seconds": None,
    }
    frame = {"policy": {"source": "kanban", "kanban_json": json.dumps(context)}}

    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.setattr(session_kanban, "_watch_submitter", lambda *_args: None)

    session_kanban.bind_worker_context(frame)

    assert os.environ["HERMES_HOME"] == str(profile_home)
    assert os.environ["HERMES_KANBAN_HOME"] == str(shared_home.resolve())
    assert Path(os.environ["HERMES_KANBAN_DB"]) == path
    assert kb.kanban_db_path(board=board).resolve() == path


def test_bound_worker_rejects_relative_kanban_home(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(shared_home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(shared_home))
    board = "shared-board"
    kb.create_board(board)
    path = kb.kanban_db_path(board=board).resolve()
    with connect_closing(path) as conn:
        task_id = kb.create_task(conn, title="shared task", assignee="code-builder")
        task = kb.claim_task(conn, task_id, claimer="private-host:owner")
        assert task is not None

    context = {
        "board": board,
        "db": str(path),
        "kanban_home": "relative/shared-home",
        "task_id": task_id,
        "run_id": task.current_run_id,
        "claim_lock": task.claim_lock,
        "workspace": str(workspace),
        "workspaces_root": str(kb.workspaces_root(board=board).resolve()),
        "profile": "code-builder",
        "branch": None,
        "tenant": None,
        "goal_mode": False,
        "max_runtime_seconds": None,
    }
    frame = {"policy": {"source": "kanban", "kanban_json": json.dumps(context)}}
    monkeypatch.setattr(session_kanban, "_watch_submitter", lambda *_args: None)

    with pytest.raises(ValueError, match="invalid_kanban_policy"):
        session_kanban.bind_worker_context(frame)


def test_pre_upgrade_canonical_policy_derives_shared_home_from_database(tmp_path):
    shared_home = tmp_path / "shared"
    board = "shared-board"
    db = shared_home / "kanban" / "boards" / board / "kanban.db"
    db.parent.mkdir(parents=True)
    db.touch()

    assert session_kanban._validated_kanban_home({"board": board, "db": str(db)}) == str(shared_home)


def test_explicit_shared_home_preserves_external_database_pin(tmp_path):
    shared_home = tmp_path / "shared"
    shared_home.mkdir()
    external_db = tmp_path / "external" / "kanban" / "boards" / "shared-board" / "kanban.db"
    external_db.parent.mkdir(parents=True)
    external_db.touch()

    assert session_kanban._validated_kanban_home({
        "kanban_home": str(shared_home), "board": "shared-board", "db": str(external_db),
    }) == str(shared_home)


def test_new_client_matches_pre_upgrade_canonical_policy(tmp_path):
    shared_home = tmp_path / "shared"
    board = "shared-board"
    db = shared_home / "kanban" / "boards" / board / "kanban.db"
    db.parent.mkdir(parents=True)
    db.touch()
    legacy = {"board": board, "db": str(db), "task_id": "t_one"}

    assert session_kanban._params_match_context(
        {"board": board, "db": str(db), "task_id": "t_one", "kanban_home": str(shared_home)}, legacy,
    )


def test_canonical_rpc_contract_accepts_shared_kanban_home():
    from tui_gateway.contracts.canonical import KanbanRunParams

    params = KanbanRunParams.model_validate({
        "board": "shared-board", "task_id": "t_one", "run_id": 7, "claim_lock": "claim",
        "db": "/shared/kanban/boards/shared-board/kanban.db", "kanban_home": "/shared",
    })

    assert params.kanban_home == "/shared"
