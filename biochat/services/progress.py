"""Public action summaries and responsive waiting updates.

Only explicit user-facing summaries are displayed; raw model reasoning,
generated code and tool output are never used as progress text.
"""

from __future__ import annotations

import ast
import re
import threading
import time
from queue import Empty, Full, Queue

from biochat.ui.sanitize import sanitize_visible_text


def public_summary(text: str) -> str:
    """Read a complete, short <progress> block outside private/action tags."""
    # Strip private blocks, including unfinished blocks during streaming.
    private = (r"think|thinking|reasoning|analysis|reflection|scratchpad|"
               r"self[-_]critique|chain[-_]of[-_]thought|inner[-_]monologue")
    text = re.sub(
        rf"<({private})\b[^>]*>.*?(?:</\1\s*>|$)", "", text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.split(r"<(?:execute|solution|observation)>", text, flags=re.I)[0]
    match = re.search(r"<progress>([^<>]{1,240})</progress>", text, re.DOTALL)
    if not match:
        return ""
    return " ".join(sanitize_visible_text(match.group(1)).split())


def execution_summary(code: str, language: str, tools: list[dict]) -> str:
    """Name registered Python tools without exposing arguments or code."""
    names = {t.get("name") for t in tools if isinstance(t, dict)}
    called = []
    if language == "python":
        try:
            tree = ast.parse(code)
            aliases = {
                alias.asname or alias.name: alias.name
                for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                for alias in node.names
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = (node.func.id if isinstance(node.func, ast.Name) else
                        node.func.attr if isinstance(node.func, ast.Attribute) else "")
                name = aliases.get(name, name)
                if name in names and name not in called and re.fullmatch(r"\w{1,80}", name):
                    called.append(name)
        except (SyntaxError, ValueError, RecursionError):
            pass
    if called:
        return "调用工具：" + "、".join(called[:4])
    return f"运行 {language.upper()} 计算，等待执行结果"


def with_wait_updates(factory, *, interval: float = 1.0):
    """Consume a blocking UI stream on one worker, emitting elapsed time.

    The worker owns the iterator (and its service locks) for its entire
    lifetime. Closing the consumer stops iteration at the next yield; it
    does not interrupt an in-flight model/tool call.
    """
    queue = Queue(maxsize=32)
    stopped = threading.Event()

    def send(item):
        while not stopped.is_set():
            try:
                queue.put(item, timeout=0.1)
                return
            except Full:
                continue

    def produce():
        iterator = None
        try:
            iterator = iter(factory())
            for event in iterator:
                if stopped.is_set():
                    break
                send(("event", event))
        except Exception as exc:
            send(("error", exc))
        finally:
            try:
                if iterator is not None and hasattr(iterator, "close"):
                    iterator.close()
            finally:
                send(("done", None))

    started = stage_started = time.monotonic()
    current = {
        "status": "initializing", "answer_so_far": "", "trace_lines": [],
        "current_step": "正在初始化 Agent 引擎",
    }

    def timed(event):
        now = time.monotonic()
        return dict(event, elapsed_seconds=int(now - started),
                    stage_elapsed_seconds=int(now - stage_started))

    worker = threading.Thread(target=produce, daemon=True, name="biochat-progress")
    worker.start()
    try:
        yield timed(current)
        while True:
            try:
                kind, item = queue.get(timeout=interval)
            except Empty:
                yield timed(current)
                continue
            if kind == "done":
                return
            if kind == "error":
                raise item
            if (item.get("status"), item.get("current_step")) != (
                current.get("status"), current.get("current_step")
            ):
                stage_started = time.monotonic()
            current = item
            yield timed(current)
            if item.get("status") in ("completed", "error"):
                return
    finally:
        stopped.set()
