"""Seed the FSD §4.2.1 example template (template-guardrails spec R2.5).

Usage: cd backend && AWS_PROFILE=... uv run python ../scripts/seed_templates.py
Requires DATABASE_URL reachable (runs anywhere the backend runs; in cloud use
an ECS exec shell or run at deploy time).
"""

import asyncio
import os
import sys

# Local dev: repo layout (scripts/ next to backend/). Container: app lives at /app.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, "/app")

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.models import Template, User  # noqa: E402

SONNET = "us.anthropic.claude-sonnet-5"
HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _guardrails(models: list[str], max_tokens: int = 8192, temp_max: float = 0.7,
                classification: str = "internal") -> dict:
    return {
        "model": {
            "allowed_models": models,
            "max_tokens": max_tokens,
            "temperature": {"min": 0.0, "max": temp_max},
            "top_p": {"min": 0.1, "max": 0.95},
        },
        "architecture": {
            "required_components": ["logging", "authentication", "error_handling"],
            "forbidden_patterns": ["public_internet_access_to_db", "hardcoded_credentials"],
        },
        "data": {"classification": classification, "pii_handling": "redact"},
        "cost": {"monthly_cap_usd": 1000, "alert_thresholds": [50, 75, 90]},
    }


TEMPLATES = [
    {
        "name": "Standard RAG Application",
        "description": "Template for building RAG-based Q&A applications with governed model usage.",
        "category": "chatbot",
        "guardrails": _guardrails([SONNET, HAIKU]),
        "scaffolding": {
            "starter_prompts": [
                "I want to build a Q&A chatbot that answers questions from our internal documents. "
                "The documents live in S3 and users are internal staff."
            ],
            "required_spec_sections": ["data_sources", "security_controls", "error_handling"],
        },
    },
    {
        "name": "Document Processing Pipeline",
        "description": "Extract structured data from uploaded documents with human review checkpoints.",
        "category": "document_processing",
        "guardrails": _guardrails([SONNET], temp_max=0.3, classification="confidential"),
        "scaffolding": {
            "starter_prompts": [
                "I need to extract structured fields from documents our team uploads "
                "(PDFs and images), with low-confidence values flagged for human review."
            ],
            "required_spec_sections": ["data_sources", "extraction_schema", "review_workflow", "error_handling"],
        },
    },
    {
        "name": "Data Analytics Assistant",
        "description": "Natural-language questions over structured data with charts and narratives.",
        "category": "data_analysis",
        "guardrails": _guardrails([SONNET, HAIKU], temp_max=0.4),
        "scaffolding": {
            "starter_prompts": [
                "I want an assistant that answers natural-language questions about our "
                "business data and renders simple charts with a short narrative."
            ],
            "required_spec_sections": ["data_sources", "query_safety", "security_controls"],
        },
    },
    {
        "name": "Workflow Automation",
        "description": "Multi-step business process automation with approvals, notifications and audit trail.",
        "category": "workflow_automation",
        "guardrails": _guardrails([SONNET], temp_max=0.3),
        "scaffolding": {
            "starter_prompts": [
                "I want to automate a multi-step approval workflow with policy checks, "
                "notifications, reminders and a full audit trail."
            ],
            "required_spec_sections": ["process_states", "approval_rules", "audit_trail", "error_handling"],
        },
    },
    {
        "name": "Content Generation Studio",
        "description": "Brand-safe content drafting with tone controls and a review lifecycle.",
        "category": "content_generation",
        "guardrails": _guardrails([SONNET, HAIKU], temp_max=0.9),
        "scaffolding": {
            "starter_prompts": [
                "I want a tool that drafts on-brand marketing copy in multiple variants, "
                "with tone controls and an approval step before anything is published."
            ],
            "required_spec_sections": ["brand_rules", "review_workflow", "content_types"],
        },
    },
    # --- Financial Services pack (owner request, 27 Jul 2026) ---
    {
        "name": "FS Maker-Checker Workflow",
        "description": "Financial-services process automation: segregation of duties, policy-rule decisioning, full audit trail — models draft, humans decide.",
        "category": "workflow_automation",
        "guardrails": _guardrails([SONNET], temp_max=0.3, classification="confidential"),
        "scaffolding": {
            "starter_prompts": [
                "I want to automate a financial-services approval process (e.g. loan "
                "triage, exception approvals) with maker-checker controls, "
                "rule-based decisioning from configuration, model-drafted summaries "
                "that humans confirm, and a complete audit trail."
            ],
            "required_spec_sections": [
                "process_states", "approval_rules", "segregation_of_duties",
                "audit_trail", "pii_handling", "error_handling",
            ],
        },
    },
    {
        "name": "FS Regulated Knowledge Assistant",
        "description": "Citation-mandatory RAG over compliance manuals and regulatory guidance; refuses on low confidence instead of guessing.",
        "category": "chatbot",
        "guardrails": _guardrails([SONNET, HAIKU], temp_max=0.4, classification="confidential"),
        "scaffolding": {
            "starter_prompts": [
                "I want a Q&A assistant over our compliance manuals and regulatory "
                "circulars where every answer must cite document, section, and "
                "effective date, and the assistant refuses with a contact pointer "
                "when it is not confident."
            ],
            "required_spec_sections": [
                "data_sources", "citation_rules", "refusal_behavior",
                "security_controls", "corpus_update_process",
            ],
        },
    },
]


async def main() -> None:
    async with SessionLocal() as db:
        admin = (
            await db.execute(select(User).where(User.role == "admin").limit(1))
        ).scalar_one_or_none()
        created, existing_count = 0, 0
        for fixture in TEMPLATES:
            existing = (
                await db.execute(select(Template).where(Template.name == fixture["name"]))
            ).scalar_one_or_none()
            if existing:
                existing_count += 1
                continue
            db.add(
                Template(
                    name=fixture["name"],
                    description=fixture["description"],
                    category=fixture["category"],
                    guardrails=fixture["guardrails"],
                    scaffolding=fixture["scaffolding"],
                    status="active",
                    created_by=admin.id if admin else None,
                )
            )
            created += 1
        await db.commit()
        print(f"templates seed: created={created} existing={existing_count} total={len(TEMPLATES)}")


if __name__ == "__main__":
    asyncio.run(main())
