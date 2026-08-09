"""Activity counters surfaced by long-running gateway heartbeats."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.tool_executor import _begin_tool_execution
from gateway.run import _format_long_running_heartbeat


def test_begin_tool_execution_increments_tool_call_count():
    agent = SimpleNamespace(
        quiet_mode=True,
        tool_progress_mode="off",
        tool_progress_callback=None,
        tool_start_callback=None,
        _current_tool=None,
        _tool_call_count=4,
        _touch_activity=MagicMock(),
    )

    _begin_tool_execution(
        agent,
        function_name="memory",
        function_args={},
        effective_task_id="task-1",
        tool_call_id="call-5",
        display_index=None,
    )

    assert agent._tool_call_count == 5
    assert agent._current_tool == "memory"


def test_long_running_heartbeat_includes_tool_count_and_wait_state():
    text = _format_long_running_heartbeat(
        6,
        {
            "tool_call_count": 12,
            "api_call_count": 7,
            "max_iterations": 90,
            "current_tool": None,
            "last_activity_desc": "waiting for non-streaming API response",
        },
        include_iteration=False,
    )

    assert text == (
        "⏳ Working — 6 min — 12 tool calls, "
        "waiting for non-streaming API response"
    )


def test_long_running_heartbeat_singular_and_verbose_iteration():
    text = _format_long_running_heartbeat(
        3,
        {
            "tool_call_count": 1,
            "api_call_count": 4,
            "max_iterations": 90,
            "current_tool": "terminal",
        },
        include_iteration=True,
    )

    assert text == "⏳ Working — 3 min — iteration 4/90, 1 tool call, terminal"
