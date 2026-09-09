"""Progress remains visible during waits without forwarding private output."""

import threading
from types import SimpleNamespace

import pytest

from biochat.core.settings import BiochatSettings
from biochat.schemas.chat import ChatRequest
from biochat.services.agent_service import BioAgentService
from biochat.services.progress import execution_summary, public_summary, with_wait_updates


def test_summary_waits_for_complete_block_and_ignores_private_content():
    assert public_summary("<progress>正在核对输入</pro") == ""
    assert public_summary("<think><progress>PRIVATE</progress>") == ""
    text = "<think>PRIVATE</think><progress>正在核对输入</progress><execute>SECRET</execute>"
    assert public_summary(text) == "正在核对输入"
    assert public_summary("<observation><progress>TOOL OUTPUT</progress></observation>") == ""
    assert public_summary("<progress>内部推理：PRIVATE</progress>") == ""


def test_registered_tool_names_without_arguments_or_arbitrary_code():
    code = "from tools import query_uniprot as query\nx = query(api_key='SECRET')\nprint(x)"
    assert execution_summary(code, "python", [{"name": "query_uniprot"}]) == "调用工具：query_uniprot"
    assert "SECRET" not in execution_summary(code, "bash", [])
    assert "print" not in execution_summary(code, "python", [])


def make_service(events):
    service = BioAgentService(BiochatSettings())
    service._initialized = True
    service._agent = SimpleNamespace(
        use_tool_retriever=False,
        tool_registry=SimpleNamespace(tools=[{"name": "query_uniprot"}]),
        go_stream=lambda *a, **k: iter(events),
    )
    return service


@pytest.mark.parametrize("stream_tokens", [False, True])
def test_summary_is_visible_before_execution_without_raw_thoughts(stream_tokens):
    text = ("<think>PRIVATE REASONING</think>"
            "<progress>正在查询 CD19 的蛋白信息</progress>"
            "<execute>query_uniprot(api_key='SECRET')</execute>")
    events = []
    if stream_tokens:
        events.extend({"type": "token", "content": char} for char in text)
    events.extend([
        {"type": "message", "output": text},
        {"type": "message", "output": "<observation>RAW SECRET LOG</observation>"},
        {"type": "message", "output": "<solution>查询完成</solution>"},
    ])
    updates = list(make_service(events).run_task_stream(ChatRequest(message="CD19")))
    summary_index = next(i for i, u in enumerate(updates)
                         if u["trace_line"] == "正在查询 CD19 的蛋白信息")
    execute_index = next(i for i, u in enumerate(updates) if u["status"] == "executing")
    assert summary_index < execute_index
    assert sum(u["trace_line"] == "正在查询 CD19 的蛋白信息" for u in updates) == 1
    assert "步骤 1" in updates[execute_index]["trace_line"]
    assert "query_uniprot" in updates[execute_index]["trace_line"]
    assert updates[-1]["answer_so_far"] == "查询完成"
    assert "PRIVATE" not in str(updates)
    assert "SECRET" not in str(updates)


def test_heartbeat_while_producer_is_blocked_and_closes_on_owner_thread(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("biochat.services.progress.time.monotonic", lambda: now[0])
    release = threading.Event()
    closed = threading.Event()
    owner = []

    def source():
        owner.append(threading.get_ident())
        try:
            yield {"status": "executing", "current_step": "查询数据库", "trace_lines": ["查询数据库"]}
            release.wait(2)
            yield {"status": "completed", "current_step": "完成"}
        finally:
            owner.append(threading.get_ident())
            closed.set()

    stream = with_wait_updates(source, interval=0.01)
    try:
        assert next(stream)["status"] == "initializing"
        assert next(stream)["current_step"] == "查询数据库"
        now[0] = 3.0
        heartbeat = next(stream)
        assert heartbeat["current_step"] == "查询数据库"
        assert heartbeat["trace_lines"] == ["查询数据库"]
        assert heartbeat["elapsed_seconds"] == 3
        assert heartbeat["stage_elapsed_seconds"] == 3
        release.set()
        assert next(stream)["status"] == "completed"
    finally:
        release.set()
        stream.close()
    assert closed.wait(2)
    assert owner[0] == owner[1] != threading.get_ident()


def test_consumer_close_stops_worker_after_current_call():
    release = threading.Event()
    closed = threading.Event()

    def source():
        try:
            yield {"status": "thinking"}
            release.wait(2)
            yield {"status": "executing"}
            pytest.fail("closed UI must not request another execution step")
        finally:
            closed.set()

    stream = with_wait_updates(source, interval=0.01)
    next(stream)
    next(stream)
    stream.close()
    release.set()
    assert closed.wait(2)


def test_worker_errors_reach_consumer():
    def source():
        raise ValueError("failed")
        yield  # pragma: no cover

    with pytest.raises(ValueError, match="failed"):
        list(with_wait_updates(source, interval=0.01))


def test_ui_preserves_summary_and_renders_wait_time(monkeypatch):
    from biochat.ui.biochat_streamlit import stream_agent_response, render_assistant_card_streaming

    service = make_service([
        {"type": "message", "output": "<progress>正在核对编号</progress><execute>pass</execute>"},
        {"type": "message", "output": "<solution>完成</solution>"},
    ])
    monkeypatch.setattr("biochat.services.agent_service.get_agent_service", lambda settings: service)
    updates = list(stream_agent_response("CD19"))
    execution = next(u for u in updates if u["status"] == "executing")
    assert "正在核对编号" in execution["current_step"]
    assert updates[-1]["status"] == "completed"
    assert any("正在核对编号" in line for line in updates[-1]["trace_lines"])
    card = render_assistant_card_streaming(
        "", execution["trace_lines"], "executing", execution["current_step"],
        elapsed_seconds=42, stage_elapsed_seconds=12,
    )
    assert "已用时 42 秒" in card
    assert "当前步骤 12 秒" in card
    assert "任务过程与方法摘要" in card
