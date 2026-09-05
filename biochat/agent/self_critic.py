"""
Self-critic node for the agent workflow.

Extracted from ``execute_self_critic()`` in the original ``a1.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from langchain_core.messages import AIMessage, HumanMessage

from biochat.agent.agent_state import AgentState

if TYPE_CHECKING:
    from biochat.agent.a1 import A1


def create_self_critic_node(
    agent: "A1", *, max_rounds: int = 1
) -> Callable[[AgentState], AgentState]:
    """Return a ``self_critic`` LangGraph node function.

    When the agent reaches ``max_rounds`` without a satisfactory solution,
    the critic generates harsh feedback and routes back to ``generate``
    for another attempt.
    """

    if max_rounds < 0:
        raise ValueError("max_rounds must be non-negative")

    def self_critic(state: AgentState) -> AgentState:
        if getattr(agent, "critic_count", 0) < max_rounds:
            feedback_prompt = (
                f"Here is a reminder of what the user requested: {agent.user_task}\n"
                f"Examine the previous executions, reasoning, and solutions.\n"
                f"Critic harshly on what could be improved.\n"
                f"Be specific and constructive.\n"
                f"Think hard about what is missing to solve the task.\n"
                f"No questions asked — just feedback."
            )

            feedback = agent.llm.invoke(
                state["messages"] + [HumanMessage(content=feedback_prompt)]
            )

            # The feedback is model-generated, not a new user instruction.
            state["messages"].append(AIMessage(
                content=f"Wait... this is not enough to solve the task. "
                        f"Here are some feedbacks for improvement:\n{feedback.content}"
            ))
            agent.critic_count += 1
            state["next_step"] = "generate"
        else:
            state["next_step"] = "end"

        return state

    return self_critic
