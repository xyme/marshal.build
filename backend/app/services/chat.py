"""Chat sessions (Postgres) + message history (DynamoDB) + prompts."""

import asyncio
import time
import uuid
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import ChatSession, User

_dynamo = None


def dynamo_table():
    global _dynamo
    if _dynamo is None:
        settings = get_settings()
        _dynamo = boto3.resource("dynamodb", region_name=settings.aws_region).Table(
            settings.dynamo_table_chat
        )
    return _dynamo


# ---------------------------------------------------------------- prompts

AWS_SERVICE_ALLOWLIST = (
    "Lambda, API Gateway, S3, DynamoDB, RDS, OpenSearch, Bedrock, Step Functions, "
    "EventBridge, SQS, SNS, CloudWatch, KMS, Cognito"
)


def chat_system_prompt(
    persona: str | None,
    template_block: str = "",
    substrate_block: str = "",
    connectors_block: str = "",
) -> str:
    tone = (
        "Use plain, non-technical English. Avoid jargon; explain concepts simply."
        if persona != "power"
        else "Use precise technical language appropriate for an engineer."
    )
    return f"""You are marshal, an assistant that helps enterprise users turn AI agent ideas into structured specifications.

Your goals in conversation:
- Understand the user's AI app idea: the problem, who will use it, what data it works with, key actions, and constraints.
- Ask targeted clarifying questions (at most 2-3 per reply) when information is missing.
- Track what you learn; when enough is known, tell the user they can generate their requirements document.
- When you make an assumption, state it explicitly and label it as an assumption.
- Only reference these AWS services when discussing architecture: {AWS_SERVICE_ALLOWLIST}. Never invent service names.
- Stay on the topic of specifying their agent. {tone}{template_block}{substrate_block}{connectors_block}"""


# Render clip for the substrate block (brownfield-substrate spec R2.3) —
# mirrors the codegen _clip discipline (16k char planning budgets).
SUBSTRATE_PROMPT_BUDGET = 16_000


def substrate_prompt_block(session: "ChatSession | None") -> str:
    """Brownfield block spliced onto the system prompt (spec R2).

    Mirrors guardrails.template_prompt_block: empty string when absent, never
    stored as a message (trimming-proof by construction). The honesty flag on
    connector consumption ships regardless of C1 status — the assistant must
    never silently promise capability parity.
    """
    text = (getattr(session, "substrate", None) or "").strip()
    if not text:
        return ""
    source = getattr(session, "substrate_source", None) or "unspecified"
    if len(text) > SUBSTRATE_PROMPT_BUDGET:
        text = text[:SUBSTRATE_PROMPT_BUDGET] + "\n[substrate truncated]"
    return f"""

BROWNFIELD MIGRATION CONTEXT: the user is migrating an EXISTING agent.
Its current definition follows, supplied verbatim by the user (source: {source}).
Treat it as current-state truth. Elicit what to keep, what to change, and what
governance to apply. Explicitly flag any capability marshal cannot reproduce
today — external tools, data sources and integrations become registered
connectors, and generated agents cannot consume connectors yet. Never silently
promise parity.
--- SUBSTRATE START ---
{text}
--- SUBSTRATE END ---"""


# ---------------------------------------------------------------- sessions (Postgres)


class PersonaModeError(Exception):
    """Persona/mode combination not allowed (FSD §4.1.1) — surfaces as 422."""


def resolve_session_mode(user: User, requested: str | None) -> str:
    """Business persona → guided, no freeform escape (guided-mode spec R1.2).

    Power/admin choose per session (default freeform).
    """
    is_business = user.role != "admin" and user.persona != "power"
    if is_business:
        if requested == "freeform":
            raise PersonaModeError(
                "Freeform chat is part of the Power User experience. Your sessions "
                "use the guided wizard; ask an admin about a persona upgrade for freeform."
            )
        return "guided"
    return requested or "freeform"


async def create_session(
    db: AsyncSession,
    user: User,
    template_id: uuid.UUID | None = None,
    mode: str | None = None,
    project_id: uuid.UUID | None = None,
) -> ChatSession:
    from app.services.guardrails import resolve_models
    from app.services.guided import initial_guided_state

    effective_mode = resolve_session_mode(user, mode)
    if project_id is not None:
        # Sessions on an existing project require editor+ (collaboration R3.2)
        from app.services import collab

        resolved = await collab.resolve_role(db, project_id, user)
        if resolved is None or not collab.role_atleast(resolved[1], "editor"):
            raise ValueError("Project not found or you lack editor access")
        project = resolved[0]
        if template_id is None and project.template_id is not None:
            template_id = project.template_id  # inherit project guardrails
    template = None
    if template_id:
        from app.models import Template

        template = await db.get(Template, template_id)
        if template is None or template.status != "active":
            raise ValueError("Template not found or not active")
    models = await resolve_models(template)
    session = ChatSession(
        user_id=user.id,
        project_id=project_id,
        model_id=models.chat,
        template_id=template.id if template else None,
        mode=effective_mode,
        guided_state=initial_guided_state() if effective_mode == "guided" else None,
        title="Guided session" if effective_mode == "guided" else "New session",
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def list_sessions(
    db: AsyncSession, user: User, *, q: str | None = None
) -> list[ChatSession]:
    """Own sessions ∪ sessions on projects where the user is editor+ (R3.2).

    Sessions are project artifacts once attached to a project; viewers do NOT
    see them (chat is an editing surface). `q` (global-search B12) filters by
    title INSIDE this statement so the deliberately-asymmetric visibility
    cannot drift between the list and search paths.
    """
    from app.models import ProjectMember

    editor_projects = (
        select(ProjectMember.project_id).where(
            ProjectMember.user_id == user.id, ProjectMember.role == "editor"
        )
    ).scalar_subquery()
    from app.core.tenant import tenant_scope

    stmt = tenant_scope(
        select(ChatSession).where(
            (ChatSession.user_id == user.id)
            | (ChatSession.project_id.in_(editor_projects))
        ),
        ChatSession,
    )
    if q:
        stmt = stmt.where(ChatSession.title.ilike(f"%{q}%"))
    result = await db.execute(stmt.order_by(ChatSession.updated_at.desc()))
    return list(result.scalars())


async def get_accessible_session(
    db: AsyncSession, user: User, session_id: uuid.UUID
) -> ChatSession | None:
    """Creator always; otherwise editor+ on the session's project (R3.2)."""
    session = await db.get(ChatSession, session_id)
    if session is None:
        return None
    if session.user_id == user.id:
        return session
    if session.project_id is None:
        return None
    from app.services import collab

    resolved = await collab.resolve_role(db, session.project_id, user)
    if resolved is not None and collab.role_atleast(resolved[1], "editor"):
        return session
    return None


def derive_title(first_message: str) -> str:
    title = " ".join(first_message.strip().split())
    return (title[:57] + "...") if len(title) > 60 else (title or "New session")


# ---------------------------------------------------------------- messages (DynamoDB)


def _sk() -> str:
    return f"{int(time.time() * 1000):013d}#{uuid.uuid4().hex[:8]}"


async def put_message(
    session_id: uuid.UUID,
    role: str,
    content: str,
    *,
    model_id: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    truncated: bool = False,
) -> dict:
    item: dict[str, Any] = {
        "session_id": str(session_id),
        "sk": _sk(),
        "role": role,
        "content": content,
    }
    if model_id:
        item["model_id"] = model_id
    if input_tokens:
        item["input_tokens"] = input_tokens
    if output_tokens:
        item["output_tokens"] = output_tokens
    if truncated:
        item["truncated"] = True
    await asyncio.to_thread(dynamo_table().put_item, Item=item)
    return item


async def get_messages(session_id: uuid.UUID) -> list[dict]:
    def query() -> list[dict]:
        items: list[dict] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("session_id").eq(str(session_id)),
            "ScanIndexForward": True,
        }
        while True:
            resp = dynamo_table().query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return items
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    return await asyncio.to_thread(query)


async def delete_messages(session_id: uuid.UUID) -> None:
    items = await get_messages(session_id)

    def batch_delete() -> None:
        with dynamo_table().batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={"session_id": item["session_id"], "sk": item["sk"]})

    await asyncio.to_thread(batch_delete)


# ---------------------------------------------------------------- context assembly


def build_converse_messages(history: list[dict], char_budget: int | None = None) -> list[dict]:
    """Convert stored messages to Bedrock converse format within a char budget.

    Keeps the most recent messages; walks backwards until the budget is exhausted
    (approximation of the FSD 100k-token sliding window).
    """
    budget = char_budget or get_settings().chat_context_char_budget
    picked: list[dict] = []
    used = 0
    for item in reversed(history):
        cost = len(item.get("content", ""))
        if picked and used + cost > budget:
            break
        picked.append(item)
        used += cost
    picked.reverse()
    messages = [
        {"role": item["role"], "content": [{"text": item["content"]}]}
        for item in picked
        if item.get("content")
    ]
    # Bedrock requires alternating roles starting with user; drop leading assistant msgs
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages
