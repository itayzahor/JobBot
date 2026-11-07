"""Gmail agent step 2 of 2: find which job in the DB a classified email is about.

This is the agent part. A single lookup can't do it: a big company can have dozens of rows (most
never applied to), Indeed's notices name only the role, and Hebrew emails name the company in Hebrew
while the tracker stores the English name. So it's a LangGraph loop - Gemini picks a search, reads the
result, decides what to try next - until it calls submit_match.

The agent reaches the DB only through the project's own MCP server (the same one Claude Desktop
uses), and only through the read tools in READ_ONLY_TOOLS. It can never change data: it outputs a
match, and a suggestion built from it waits for the user's Accept.

langchain-mcp-adapters isn't used because it pins mcp<2 and the server is written for mcp 2.x; the
adapter's job (MCP tool -> LangChain tool) is the 20 lines in mcp_tools() below.
"""

import json
import os
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool, tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from mcp.client.session import ClientSession
from pydantic import BaseModel

from jobbot.filters import gemini_filter as gf  # also loads .env (GEMINI_API_KEY)
from jobbot.gmail_classifier import EmailClassification, strip_quoted

# The only MCP tools the agent gets - an allowlist, so a tool added to the server later never
# reaches the agent by accident
READ_ONLY_TOOLS = {"search_jobs", "get_job"}

# Each agent<->tools round trip is 2 graph steps; 16 allows ~7 searches before giving up
MAX_GRAPH_STEPS = 16

SYSTEM_INSTRUCTION = """You match an email about a job application to the job in the user's job \
tracker. You get the email's classification (type, company, role, job reference) and its start.

Search with search_jobs, then call submit_match exactly once with your answer:
- match="job": the email is about one specific job. Put its id in job_ids. If the tracker has \
the same job twice (identical company+title rows - a duplicate scrape), include every duplicate id.
- match="ambiguous": several of the user's applications fit and the email gives no way to tell \
which (no role, or a role shared by several). Put all fitting ids in job_ids.
- match="not_in_db": no job in the tracker fits - the user applied outside the tracker. job_ids=[].

How to search:
- Start with the company. If nothing comes back, retry with variants: a shorter name ("Acme" for \
"Acme Software Technologies"), without Ltd/Inc, the English name for a Hebrew one \
(e.g. אקמה -> Acme), or a distinctive word of the role in the title filter.
- The tracker stores many jobs the user never applied to. Prefer status applied or process. A \
new/deleted/rejected job fits only if its title clearly matches the role and no applied job does \
(the user applied outside the dashboard).
- Titles differ slightly between the posting and the email ("Engineer Software" vs "Software \
Engineer - Graduate Program") - judge by meaning, use job_reference and get_job's description \
when titles are close.
- Use dates. The user marks a job applied on the day they apply, so for an applied job, \
last_changed is roughly the application date. An email can only be about an application made on \
or before the email date: an applied job whose last_changed is several days AFTER the email is a \
later, different application (often a repost of the same role) - not this email's job. A \
confirmation usually arrives within a day or two of last_changed.
- A job reference beats dates and titles. If the email's job_reference (JR2020001, ID 10000001, \
R10001) appears in a job's description, that is the job - even if it was scraped after the email \
(a repost of the same job id). If the job's description shows a DIFFERENT reference, it is not \
the job.
- No company (Indeed's "הגשת מועמדות דרך Indeed" notices name only the role): search by title, \
then prefer an applied job whose last_changed is within a few days of the email date.
- A different role at the same company is NOT a match - answer not_in_db.
- Never invent ids - only ids that a search returned."""


class MatchResult(BaseModel):
    match: Literal["job", "ambiguous", "not_in_db"]
    job_ids: list[str]
    reason: str


@tool(args_schema=MatchResult)
def submit_match(match: str, job_ids: list[str], reason: str) -> str:
    """Submit the final answer. Call exactly once, when you're done searching."""
    return "submitted"


def mcp_tools(session: ClientSession, tool_defs) -> list[StructuredTool]:
    """Wrap the server's read-only MCP tools as LangChain tools that call it over the open session.

    Input: an initialized MCP ClientSession, and its list_tools().tools.
    Output: LangChain tools named and documented exactly like the MCP tools.
    """
    tools = []
    for t in tool_defs:
        if t.name not in READ_ONLY_TOOLS:
            continue

        async def call(_name=t.name, **kwargs) -> str:
            result = await session.call_tool(_name, kwargs)
            if result.is_error:
                return f"Error: {result.content[0].text}"
            data = result.structured_content
            if data is None:
                return result.content[0].text
            return json.dumps(data.get("result", data), ensure_ascii=False)

        tools.append(StructuredTool.from_function(
            coroutine=call, name=t.name, description=t.description, args_schema=t.input_schema,
        ))
    return tools


def build_agent(tools: list[StructuredTool]):
    """The matching graph: agent -> (tools -> agent)* -> END once submit_match is called.

    Input: the MCP tools from mcp_tools().
    Output: a compiled LangGraph graph; invoke with {"messages": [...]}.
    """
    llm = ChatGoogleGenerativeAI(
        model=gf.MODEL, api_key=os.environ["GEMINI_API_KEY"], temperature=0, max_retries=3,
    )
    # tool_choice="any": every turn must be a tool call, so the run always ends in submit_match
    # rather than a free-text answer
    llm = llm.bind_tools([*tools, submit_match], tool_choice="any")

    async def agent(state: MessagesState) -> dict:
        return {"messages": [await llm.ainvoke(state["messages"])]}

    def route(state: MessagesState) -> str:
        last = state["messages"][-1]
        if any(call["name"] == "submit_match" for call in last.tool_calls):
            return END
        return "tools"

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["tools", END])
    graph.add_edge("tools", "agent")
    return graph.compile()


async def match_email(agent, email: dict, classification: EmailClassification) -> tuple[MatchResult, dict]:
    """Run the agent on one email.

    Input: the compiled agent, the email dict (sender, subject, received, body) and its
    classification.
    Output: (MatchResult, stats) - stats has the tool calls made and total tokens, for the eval.
    A run that hits MAX_GRAPH_STEPS without answering returns not_in_db with a reason saying so.
    """
    # Outreach is about a role the user hasn't applied to - there's no application to find, and
    # the agent was seen attaching referral invitations to unrelated open applications
    if classification.type == "recruiter_outreach":
        return MatchResult(match="not_in_db", job_ids=[], reason="recruiter outreach - not about an existing application"), {"calls": [], "tokens": 0}

    prompt = (
        f"Email date: {email['received'][:10]}\nFrom: {email['sender']}\nSubject: {email['subject']}\n\n"
        f"Classification: type={classification.type}, company={classification.company!r}, "
        f"role={classification.role!r}, job_reference={classification.job_reference!r}\n\n"
        f"Email start:\n{strip_quoted(email['body'])[:1500]}"
    )
    messages = [SystemMessage(SYSTEM_INSTRUCTION), HumanMessage(prompt)]
    try:
        state = await agent.ainvoke({"messages": messages}, {"recursion_limit": MAX_GRAPH_STEPS})
    except GraphRecursionError:
        return MatchResult(match="not_in_db", job_ids=[], reason="gave up: step limit reached"), {"calls": [], "tokens": 0}

    ai_messages = [m for m in state["messages"] if isinstance(m, AIMessage)]
    calls = [f"{c['name']}({json.dumps(c['args'], ensure_ascii=False)})" for m in ai_messages for c in m.tool_calls]
    tokens = sum((m.usage_metadata or {}).get("total_tokens", 0) for m in ai_messages)
    final = next(c for c in ai_messages[-1].tool_calls if c["name"] == "submit_match")
    return MatchResult(**final["args"]), {"calls": calls, "tokens": tokens}
