"""Marketplace seed fixtures — 10 curated samples across the six categories.

Deterministic canned content (no Bedrock): committed so seeding is free,
repeatable, and testable (marketplace spec: fixtures reused by tests).
Each spec_snapshot doc is compact but structurally real (user stories,
acceptance criteria, mermaid design, KIRO task checklists).
"""


def _req(title: str, stories: list[tuple[str, str, str]], extra_reqs: list[str]) -> str:
    lines = [f"# {title} — Requirements", "", "## User Stories", ""]
    for i, (persona, want, why) in enumerate(stories, 1):
        lines.append(f"- US-{i}: As a {persona}, I want to {want} so that {why}.")
    lines += ["", "## Functional Requirements", ""]
    lines += [f"- FR-{i}: {r}" for i, r in enumerate(extra_reqs, 1)]
    lines += [
        "",
        "## Assumptions",
        "",
        "- Runs in an AWS sandbox account provisioned by marshal",
        "- Authentication is handled by the platform (no app-level login)",
        "- English-language content only for the first iteration",
    ]
    return "\n".join(lines)


def _design(title: str, components: list[str], flow: list[str]) -> str:
    mermaid = ["```mermaid", "graph TD"]
    for i, step in enumerate(flow):
        mermaid.append(f'    N{i}["{step}"]')
        if i:
            mermaid.append(f"    N{i-1} --> N{i}")
    mermaid.append("```")
    lines = [f"# {title} — Design", "", "## Architecture", ""] + mermaid + ["", "## Components", ""]
    lines += [f"- **{c.split(':')[0]}**: {c.split(':', 1)[1].strip()}" for c in components]
    lines += [
        "",
        "## Data Handling",
        "",
        "- All data stays inside the sandbox account",
        "- S3 objects encrypted at rest (SSE-S3); no PII leaves the VPC",
    ]
    return "\n".join(lines)


def _tasks(title: str, tasks: list[str]) -> str:
    lines = [f"# {title} — Implementation Plan", ""]
    lines += [f"- [ ] {i}. {t}" for i, t in enumerate(tasks, 1)]
    return "\n".join(lines)


SONNET = "us.anthropic.claude-sonnet-5"
HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

SAMPLES: list[dict] = [
    {
        "title": "Policy Q&A Bot",
        "description": "RAG chatbot that answers employee questions from company policy documents with citations.",
        "category": "chatbot",
        "complexity": "beginner",
        "models_used": [SONNET, HAIKU],
        "keywords": ["rag", "chatbot", "policies", "hr", "knowledge-base", "citations"],
        "metadata_extra": {"est_build_usd": 1.5, "est_run_usd_month": 45, "tech_stack": ["Lambda", "OpenSearch Serverless", "S3", "Bedrock"]},
        "long_description": (
            "A retrieval-augmented chatbot for internal policy questions. Documents are "
            "ingested from S3, chunked and embedded, and answers always cite the source "
            "section so employees can verify. Includes a small admin page for re-indexing.\n\n"
            "**Good first fork if** you have a folder of PDFs/wikis and want trustworthy Q&A."
        ),
        "requirements": _req(
            "Policy Q&A Bot",
            [
                ("employee", "ask policy questions in plain language", "I get instant answers without reading full documents"),
                ("employee", "see source citations on every answer", "I can trust and verify the response"),
                ("HR admin", "upload or replace policy documents", "answers stay current"),
            ],
            [
                "Ingest PDF/Markdown documents from an S3 bucket into a vector index",
                "Answer questions using retrieved chunks only; refuse when confidence is low",
                "Every answer lists document title + section of its sources",
                "Re-index on demand via an admin action",
            ],
        ),
        "design": _design(
            "Policy Q&A Bot",
            [
                "Ingestion Lambda: chunks documents, writes embeddings to the vector store",
                "Query API: Lambda behind API Gateway; retrieval + Bedrock answer synthesis",
                "Web UI: single-page chat with citation cards",
                "Vector store: OpenSearch Serverless collection",
            ],
            ["User question", "Retrieve top-k chunks", "Bedrock synthesis with citations", "Answer + sources"],
        ),
        "tasks": _tasks(
            "Policy Q&A Bot",
            [
                "Provision S3 bucket, OpenSearch Serverless collection, IAM roles",
                "Ingestion Lambda: parse, chunk (500 tokens, 50 overlap), embed, index",
                "Query Lambda: embed question, retrieve top 5, build grounded prompt",
                "Chat UI with citation cards and confidence fallback message",
                "Admin re-index endpoint + smoke tests",
            ],
        ),
    },
    {
        "title": "Invoice Parser",
        "description": "Extracts structured data from uploaded invoices into a reviewable table with CSV export.",
        "category": "document_processing",
        "complexity": "intermediate",
        "models_used": [SONNET],
        "keywords": ["invoices", "extraction", "ocr", "finance", "structured-data"],
        "metadata_extra": {"est_build_usd": 2.0, "est_run_usd_month": 60, "tech_stack": ["Lambda", "Textract", "DynamoDB", "S3", "Bedrock"]},
        "long_description": (
            "Upload invoices (PDF or image), get vendor, dates, line items, and totals as "
            "structured rows. Low-confidence fields are flagged for human review. Approved "
            "rows export to CSV for the finance system."
        ),
        "requirements": _req(
            "Invoice Parser",
            [
                ("AP clerk", "upload a batch of invoices", "I avoid manual data entry"),
                ("AP clerk", "review flagged fields side-by-side with the source", "errors are caught before export"),
                ("finance manager", "export approved invoices as CSV", "our ERP import stays unchanged"),
            ],
            [
                "Accept PDF/PNG/JPG up to 10MB via presigned upload",
                "Extract vendor, invoice number, dates, line items, totals as JSON",
                "Confidence < 0.8 marks a field for review; UI highlights source region",
                "Approval locks the record; CSV export covers a selected date range",
            ],
        ),
        "design": _design(
            "Invoice Parser",
            [
                "Upload flow: presigned S3 PUT + EventBridge trigger",
                "Extract Lambda: Textract raw text/layout → Bedrock schema-constrained JSON",
                "Review API + UI: table with per-field confidence chips",
                "Store: DynamoDB single-table (invoice, line items, audit of edits)",
            ],
            ["Upload to S3", "Textract layout", "Bedrock field extraction", "Review UI", "CSV export"],
        ),
        "tasks": _tasks(
            "Invoice Parser",
            [
                "S3 + presigned upload endpoint + EventBridge rule",
                "Extraction Lambda: Textract → prompt-constrained JSON (retry on schema fail)",
                "DynamoDB model + review/approve endpoints",
                "Review UI table with confidence highlighting and edit trail",
                "CSV export endpoint + integration test with 5 fixture invoices",
            ],
        ),
    },
    {
        "title": "Sales Insights Analyst",
        "description": "Natural-language questions over weekly sales CSVs with charts and trend narratives.",
        "category": "data_analysis",
        "complexity": "intermediate",
        "models_used": [SONNET, HAIKU],
        "keywords": ["analytics", "sales", "nlq", "charts", "trends", "csv"],
        "metadata_extra": {"est_build_usd": 1.8, "est_run_usd_month": 50, "tech_stack": ["Lambda", "Athena", "S3", "QuickSight-lite UI", "Bedrock"]},
        "long_description": (
            "Drop weekly sales exports into S3 and ask questions like \"which region grew "
            "fastest last month?\". The app translates questions to SQL, runs them on Athena, "
            "and narrates the result with a small chart."
        ),
        "requirements": _req(
            "Sales Insights Analyst",
            [
                ("sales ops analyst", "ask sales questions in natural language", "I skip writing SQL"),
                ("sales manager", "get a weekly digest of notable trends", "I act on changes early"),
            ],
            [
                "Map NL questions to SQL over a fixed sales schema (guard against unsafe SQL)",
                "Render bar/line charts for grouped results",
                "Weekly scheduled digest summarising top movers",
                "Refuse questions outside the sales dataset",
            ],
        ),
        "design": _design(
            "Sales Insights Analyst",
            [
                "Query planner: Bedrock NL→SQL with allowlisted tables/functions",
                "Executor: Athena query + result-shape detection for chart choice",
                "Digest Lambda: EventBridge cron, trend detection, email/SNS out",
                "UI: question box, history, chart canvas",
            ],
            ["NL question", "SQL generation + validation", "Athena execution", "Narrative + chart"],
        ),
        "tasks": _tasks(
            "Sales Insights Analyst",
            [
                "Glue table over sales CSVs + Athena workgroup",
                "NL→SQL prompt with schema card + SQL validator (sqlglot allowlist)",
                "Result narrator + chart-type heuristic",
                "Weekly digest cron with top-3 movers",
                "UI with query history and saved questions",
            ],
        ),
    },
    {
        "title": "Approval Workflow Copilot",
        "description": "Routes purchase requests through policy checks and approver chains with AI-drafted justifications.",
        "category": "workflow_automation",
        "complexity": "advanced",
        "models_used": [SONNET],
        "keywords": ["approvals", "workflow", "procurement", "policy", "routing"],
        "metadata_extra": {"est_build_usd": 2.5, "est_run_usd_month": 70, "tech_stack": ["Step Functions", "Lambda", "DynamoDB", "SES", "Bedrock"]},
        "long_description": (
            "Submit a purchase request; the copilot checks it against spend policy, drafts the "
            "justification, routes to the right approver chain, and chases responses. Full "
            "audit trail of every decision."
        ),
        "requirements": _req(
            "Approval Workflow Copilot",
            [
                ("requester", "submit a purchase request with attachments", "approvals start immediately"),
                ("approver", "see policy-check results and an AI summary", "decisions take one minute"),
                ("auditor", "export the full decision trail", "compliance reviews are painless"),
            ],
            [
                "Policy engine: amount thresholds, category rules, vendor allowlist",
                "AI drafts justification summary from request + attachments",
                "Approver chain resolution by amount and department",
                "Reminder nudges at 24h/72h; escalation after 5 business days",
                "Immutable event log per request",
            ],
        ),
        "design": _design(
            "Approval Workflow Copilot",
            [
                "Intake API: request validation + S3 attachment store",
                "Step Functions: policy check → summarise → route → wait/remind → close",
                "Policy Lambda: rule table in DynamoDB, versioned",
                "Notifier: SES templated emails with signed action links",
            ],
            ["Request submitted", "Policy checks", "AI summary", "Approver chain", "Decision + audit log"],
        ),
        "tasks": _tasks(
            "Approval Workflow Copilot",
            [
                "DynamoDB tables: requests, policy rules, event log",
                "Step Functions state machine with wait/timeout states",
                "Policy check Lambda + rule admin endpoints",
                "Bedrock summary step with attachment text extraction",
                "SES action links (approve/reject) with one-time tokens",
                "Audit export endpoint (JSON/CSV)",
            ],
        ),
    },
    {
        "title": "Content Writer Studio",
        "description": "Brand-voice marketing copy generator with tone presets, variants, and approval workflow.",
        "category": "content_generation",
        "complexity": "beginner",
        "models_used": [SONNET, HAIKU],
        "keywords": ["marketing", "copywriting", "brand-voice", "variants", "social"],
        "metadata_extra": {"est_build_usd": 1.2, "est_run_usd_month": 35, "tech_stack": ["Lambda", "DynamoDB", "S3", "Bedrock"]},
        "long_description": (
            "Give it a brief, get three on-brand variants for blog intros, social posts, or "
            "product blurbs. Brand voice is configured once (tone sliders + example passages) "
            "and enforced on every generation."
        ),
        "requirements": _req(
            "Content Writer Studio",
            [
                ("marketer", "generate three variants from a short brief", "I pick and refine quickly"),
                ("brand lead", "configure tone and reference passages once", "all output stays on brand"),
                ("marketer", "send a variant for approval", "published copy is signed off"),
            ],
            [
                "Brand profile: tone sliders, banned phrases, 3 reference passages",
                "Generate 3 variants per brief with per-variant regenerate",
                "Draft → review → approved lifecycle with comments",
                "History searchable by campaign tag",
            ],
        ),
        "design": _design(
            "Content Writer Studio",
            [
                "Brand profile store: DynamoDB, versioned",
                "Generation API: prompt assembly (brief + brand block) → 3 parallel calls",
                "Review flow: status field + comment thread",
                "UI: brief form, variant cards, diff-against-previous",
            ],
            ["Brief", "Brand-voice prompt assembly", "3 variants", "Review + approve"],
        ),
        "tasks": _tasks(
            "Content Writer Studio",
            [
                "Brand profile CRUD + prompt block builder",
                "Variant generation endpoint (parallel calls, seed diversity)",
                "Review lifecycle + comments",
                "UI: brief form, cards, approval badge",
                "Banned-phrase post-filter + tests",
            ],
        ),
    },
    {
        "title": "Compliance Checker",
        "description": "Scans contracts against a clause playbook and flags deviations with suggested redlines.",
        "category": "document_processing",
        "complexity": "advanced",
        "models_used": [SONNET],
        "keywords": ["legal", "contracts", "clauses", "redlines", "playbook", "risk"],
        "metadata_extra": {"est_build_usd": 2.8, "est_run_usd_month": 80, "tech_stack": ["Lambda", "S3", "DynamoDB", "Bedrock"]},
        "long_description": (
            "Upload a third-party contract and get a clause-by-clause comparison against your "
            "playbook: matches, deviations, missing protections, and suggested redline text "
            "for each finding. Findings export to a review memo."
        ),
        "requirements": _req(
            "Compliance Checker",
            [
                ("in-house counsel", "scan a contract against our playbook", "first-pass review takes minutes"),
                ("in-house counsel", "get suggested redline language per deviation", "markups start from a strong draft"),
                ("legal ops", "maintain the clause playbook", "standards evolve without code changes"),
            ],
            [
                "Playbook: clause categories with preferred/fallback/unacceptable positions",
                "Clause segmentation + classification of uploaded contracts",
                "Deviation scoring with rationale and suggested redline per finding",
                "Review memo export (Markdown/PDF-ready)",
            ],
        ),
        "design": _design(
            "Compliance Checker",
            [
                "Playbook store: DynamoDB with clause taxonomy",
                "Pipeline: segment → classify → compare (per clause, schema-constrained JSON)",
                "Findings API: severity buckets, accept/dismiss states",
                "Memo builder: findings → structured Markdown",
            ],
            ["Contract upload", "Clause segmentation", "Playbook comparison", "Findings + redlines", "Review memo"],
        ),
        "tasks": _tasks(
            "Compliance Checker",
            [
                "Playbook schema + admin CRUD",
                "Segmentation prompt + heading heuristics",
                "Per-clause comparison with strict JSON contract + retries",
                "Findings UI with severity filters and accept/dismiss",
                "Memo export + golden-file tests on 3 fixture contracts",
            ],
        ),
    },
    {
        "title": "Meeting Minutes Summarizer",
        "description": "Turns meeting transcripts into structured minutes: decisions, action items, owners, due dates.",
        "category": "document_processing",
        "complexity": "beginner",
        "models_used": [HAIKU, SONNET],
        "keywords": ["meetings", "minutes", "transcripts", "action-items", "summaries"],
        "metadata_extra": {"est_build_usd": 0.9, "est_run_usd_month": 25, "tech_stack": ["Lambda", "S3", "DynamoDB", "Bedrock"]},
        "long_description": (
            "Paste or upload a transcript; receive clean minutes with decisions, action items "
            "(owner + due date), and open questions. Action items sync to a simple tracker "
            "with completion states."
        ),
        "requirements": _req(
            "Meeting Minutes Summarizer",
            [
                ("team lead", "get structured minutes from a transcript", "nobody writes them manually"),
                ("member", "see my action items across meetings", "nothing falls through"),
            ],
            [
                "Accept pasted text or uploaded VTT/TXT up to 2MB",
                "Extract decisions, action items (owner, due date), open questions",
                "Action tracker with done/undone and per-person filter",
                "Skip small talk; preserve technical terms verbatim",
            ],
        ),
        "design": _design(
            "Meeting Minutes Summarizer",
            [
                "Extraction: two-pass (fast pass Haiku, structure pass Sonnet on long inputs)",
                "Tracker: DynamoDB action items keyed by person",
                "UI: paste box, minutes view, tracker tab",
            ],
            ["Transcript in", "Extraction passes", "Structured minutes", "Action tracker"],
        ),
        "tasks": _tasks(
            "Meeting Minutes Summarizer",
            [
                "Upload/paste endpoint + size guards",
                "Extraction prompts + JSON schema validation",
                "Tracker CRUD + per-person view",
                "Minutes UI with copy-as-Markdown",
            ],
        ),
    },
    {
        "title": "Customer Feedback Triage",
        "description": "Classifies inbound feedback by theme and sentiment, clusters duplicates, and drafts responses.",
        "category": "data_analysis",
        "complexity": "beginner",
        "models_used": [HAIKU],
        "keywords": ["feedback", "sentiment", "triage", "support", "clustering"],
        "metadata_extra": {"est_build_usd": 1.0, "est_run_usd_month": 30, "tech_stack": ["Lambda", "DynamoDB", "S3", "Bedrock"]},
        "long_description": (
            "Point a webhook or CSV import at it; every piece of feedback gets theme, "
            "sentiment, urgency, and a suggested reply draft. A weekly rollup shows the top "
            "themes trending up."
        ),
        "requirements": _req(
            "Customer Feedback Triage",
            [
                ("support lead", "see feedback grouped by theme and urgency", "the team works the right queue"),
                ("agent", "start from an AI reply draft", "response time drops"),
                ("PM", "see weekly theme trends", "roadmap reflects reality"),
            ],
            [
                "Ingest via webhook POST and CSV import",
                "Classify theme (configurable taxonomy), sentiment, urgency",
                "Near-duplicate grouping within a rolling 7-day window",
                "Reply drafts respect a configurable tone guide",
            ],
        ),
        "design": _design(
            "Customer Feedback Triage",
            [
                "Ingest: webhook API + CSV importer",
                "Classifier: single fast-model call with taxonomy card",
                "Grouper: cosine over embeddings, threshold clustering",
                "Rollup cron: weekly theme deltas",
            ],
            ["Feedback in", "Classify", "Cluster", "Queue + drafts", "Weekly rollup"],
        ),
        "tasks": _tasks(
            "Customer Feedback Triage",
            [
                "Webhook + CSV ingest with dedupe keys",
                "Taxonomy config + classification prompt",
                "Embedding clustering job",
                "Queue UI with draft panel",
                "Weekly rollup + trend chart",
            ],
        ),
    },
    {
        "title": "Onboarding Buddy",
        "description": "Conversational new-hire assistant that answers setup questions and tracks day-1/week-1 checklists.",
        "category": "chatbot",
        "complexity": "intermediate",
        "models_used": [SONNET, HAIKU],
        "keywords": ["onboarding", "hr", "new-hire", "checklist", "assistant"],
        "metadata_extra": {"est_build_usd": 1.6, "est_run_usd_month": 40, "tech_stack": ["Lambda", "DynamoDB", "S3", "Bedrock"]},
        "long_description": (
            "A friendly assistant for new joiners: answers IT/HR setup questions from your "
            "onboarding docs (RAG), tracks per-role checklists, and pings the buddy/manager "
            "when someone is stuck on a step for too long."
        ),
        "requirements": _req(
            "Onboarding Buddy",
            [
                ("new hire", "ask setup questions any time", "I'm unblocked without waiting for people"),
                ("new hire", "see my day-1/week-1 checklist with progress", "I know exactly what's next"),
                ("manager", "get alerted when a hire is stuck", "I intervene early"),
            ],
            [
                "RAG over onboarding documents with citations",
                "Role-based checklist templates with step completion",
                "Stuck detection: no progress on a step for 48h → notify buddy",
                "Escalate to a human when the bot is unsure twice in a row",
            ],
        ),
        "design": _design(
            "Onboarding Buddy",
            [
                "RAG core: shared with Policy Q&A pattern (S3 → vectors → grounded answers)",
                "Checklist engine: templates by role, instance per hire",
                "Nudger: daily cron scanning stalled steps",
                "Chat UI: conversation pane with checklist sidebar",
            ],
            ["Question or checklist view", "Grounded answer / step update", "Stuck detection", "Buddy notification"],
        ),
        "tasks": _tasks(
            "Onboarding Buddy",
            [
                "Reuse RAG ingestion for onboarding docs",
                "Checklist templates + instance CRUD",
                "Chat endpoint with unsure-twice escalation rule",
                "Stalled-step cron + notification",
                "UI: chat + checklist sidebar",
            ],
        ),
    },
    {
        "title": "Release Notes Generator",
        "description": "Drafts customer-facing release notes from merged PR titles and issue labels each sprint.",
        "category": "content_generation",
        "complexity": "intermediate",
        "models_used": [SONNET],
        "keywords": ["release-notes", "changelog", "engineering", "devrel", "automation"],
        "metadata_extra": {"est_build_usd": 1.1, "est_run_usd_month": 20, "tech_stack": ["Lambda", "EventBridge", "S3", "Bedrock"]},
        "long_description": (
            "Connect a repo export (CSV/JSON of merged PRs). Each sprint the generator groups "
            "changes into Features/Improvements/Fixes, rewrites titles for customers, drops "
            "internal-only items, and produces Markdown ready for the docs site."
        ),
        "requirements": _req(
            "Release Notes Generator",
            [
                ("product manager", "get a customer-ready draft each sprint", "release comms take minutes"),
                ("engineer", "have internal-only changes excluded automatically", "nothing confidential leaks"),
            ],
            [
                "Ingest merged-PR export (title, labels, body excerpt)",
                "Group into Features/Improvements/Fixes by labels + content",
                "Rewrite entries in customer voice; exclude `internal` label",
                "Output Markdown section per release with anchors",
            ],
        ),
        "design": _design(
            "Release Notes Generator",
            [
                "Importer: CSV/JSON upload or scheduled S3 pickup",
                "Grouper+writer: one structured call per release",
                "Store: releases + entries with edit-before-publish",
                "Export: Markdown artifact to S3 with stable anchors",
            ],
            ["PR export", "Group + rewrite", "Editable draft", "Markdown artifact"],
        ),
        "tasks": _tasks(
            "Release Notes Generator",
            [
                "Import parsing + label mapping config",
                "Structured generation with per-entry source links",
                "Draft editor endpoints",
                "Markdown exporter + anchor scheme",
                "Fixture-based golden tests",
            ],
        ),
    },
]


def snapshot_for(sample: dict) -> dict:
    return {
        "requirements_md": sample["requirements"],
        "design_md": sample["design"],
        "tasks_md": sample["tasks"],
    }
