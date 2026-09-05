"""Regression tests for the core agent control loop."""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

from biochat.agent.self_critic import create_self_critic_node
from biochat.agent.workflow import create_generation_node


class SequenceLLM:
    def __init__(self, *responses: str):
        self.responses = iter(responses)

    def invoke(self, messages):
        return AIMessage(content=next(self.responses))


def test_generation_stops_after_two_format_retries():
    agent = SimpleNamespace(
        system_prompt="test",
        llm=SequenceLLM("invalid one", "invalid two", "invalid three"),
    )
    generate = create_generation_node(agent)
    state = {
        "messages": [HumanMessage(content="question")],
        "next_step": None,
        "session_id": "test",
    }

    assert generate(state)["next_step"] == "generate"
    assert generate(state)["next_step"] == "generate"
    assert generate(state)["next_step"] == "end"
    assert "terminated" in state["messages"][-1].content.lower()


def test_self_critic_runs_configured_number_of_rounds():
    agent = SimpleNamespace(
        critic_count=0,
        user_task="answer the question",
        llm=SequenceLLM("check the evidence"),
    )
    critic = create_self_critic_node(agent, max_rounds=1)
    state = {
        "messages": [
            HumanMessage(content="question"),
            AIMessage(content="<solution>first answer</solution>"),
        ],
        "next_step": "end",
        "session_id": "test",
    }

    assert critic(state)["next_step"] == "generate"
    assert agent.critic_count == 1
    assert isinstance(state["messages"][-1], AIMessage)
    assert "check the evidence" in state["messages"][-1].content
    assert critic(state)["next_step"] == "end"
