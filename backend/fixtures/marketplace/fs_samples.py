"""Financial Services marketplace seed pack (27 Jul 2026).

Six curated FS samples reusing the deterministic fixture helpers — catalog
content only (content policy: no synthetic usage data). Generic
institution-neutral scenarios; every sample leans on the platform's
governance posture (human-in-the-loop, audit trails, PII redaction,
data never leaves the Enclave account).
"""

from fixtures.marketplace.samples import HAIKU, SONNET, _design, _req, _tasks

FS_SAMPLES: list[dict] = [
    {
        "title": "KYC Onboarding Assistant",
        "description": "Extracts identity data from onboarding documents, runs completeness checks, and queues cases for compliance review.",
        "category": "document_processing",
        "complexity": "intermediate",
        "models_used": [SONNET],
        "keywords": ["kyc", "onboarding", "compliance", "identity", "extraction", "financial-services"],
        "metadata_extra": {
            "est_build_usd": 2.5, "est_run_usd_month": 80,
            "tech_stack": ["Lambda", "Textract", "DynamoDB", "S3", "Bedrock"],
        },
        "long_description": (
            "Turns a folder of onboarding documents (IDs, proof of address, corporate "
            "registries) into structured KYC profiles. Every extracted field carries a "
            "confidence score; anything below threshold routes to a human review queue "
            "with the source snippet alongside. Screening checklist output is designed "
            "to hand off to your existing sanctions/PEP tooling — this sample does NOT "
            "make screening decisions itself.\n\n"
            "**Good first fork if** your onboarding team re-keys documents into a case "
            "system today."
        ),
        "requirements": _req(
            "KYC Onboarding Assistant",
            [
                ("onboarding officer", "upload a client document pack", "profiles are drafted without manual re-keying"),
                ("compliance reviewer", "see low-confidence fields flagged with source snippets", "I verify quickly and defensibly"),
                ("MLRO", "export a case file with full extraction provenance", "the audit trail satisfies internal review"),
            ],
            [
                "Extract identity fields (name, DOB, nationality, document numbers, expiry) from PDFs and images",
                "Score field confidence; below-threshold fields require human confirmation before the profile completes",
                "Produce a screening-ready checklist (names, aliases, jurisdictions) for downstream sanctions/PEP tools",
                "Record who confirmed what and when; case file export includes provenance per field",
                "Redact PII from application logs; documents never leave the account",
            ],
        ),
        "design": _design(
            "KYC Onboarding Assistant",
            [
                "Intake Lambda: virus-scan stub, classify document type, store to S3 (SSE)",
                "Extraction Lambda: Textract + Bedrock field normalization with confidence scores",
                "Review API: queue of below-threshold fields; confirm/correct actions are audited",
                "Case store: DynamoDB profiles with per-field provenance",
            ],
            ["Document pack upload", "Classify + extract with confidence", "Human review of flagged fields", "Screening checklist + case export"],
        ),
        "tasks": _tasks(
            "KYC Onboarding Assistant",
            [
                "Provision S3 (SSE), DynamoDB case table, IAM roles",
                "Intake Lambda: type classification, storage layout per case",
                "Extraction Lambda: Textract parse + Bedrock normalization to the KYC schema with confidence",
                "Review queue API + confirm/correct endpoints with audit records",
                "Case export (JSON/CSV) with per-field provenance + smoke tests",
            ],
        ),
    },
    {
        "title": "Loan Application Triage",
        "description": "Routes loan applications through completeness checks and policy-based risk banding into a maker-checker approval flow.",
        "category": "workflow_automation",
        "complexity": "intermediate",
        "models_used": [SONNET],
        "keywords": ["lending", "credit", "triage", "maker-checker", "approvals", "financial-services"],
        "metadata_extra": {
            "est_build_usd": 2.5, "est_run_usd_month": 70,
            "tech_stack": ["Lambda", "Step Functions", "DynamoDB", "API Gateway", "Bedrock"],
        },
        "long_description": (
            "An intake-to-decision workflow for loan applications: completeness "
            "validation, document checklist, policy-rule risk banding (the rules are "
            "yours, in configuration — the model drafts summaries and never decides), "
            "then maker-checker approval with a full state history. Mirrors the "
            "platform's own governance pattern: low-band applications fast-track, "
            "higher bands require a second approver."
        ),
        "requirements": _req(
            "Loan Application Triage",
            [
                ("loan officer", "submit an application with its document checklist", "intake is consistent and nothing is missed"),
                ("credit approver", "review a drafted summary with the full source file", "decisions are faster but stay mine"),
                ("head of credit", "see every state change with actor and timestamp", "the process stands up to audit"),
            ],
            [
                "Validate application completeness against a configurable checklist before triage",
                "Band applications by configured policy rules (amount, tenor, exposure); banding is rule-based, model output is advisory summary only",
                "Maker-checker: higher bands require a second approver distinct from the preparer",
                "Every transition (submitted, banded, approved, rejected, returned) is recorded with actor + timestamp",
                "Applicant PII is masked in notifications and logs",
            ],
        ),
        "design": _design(
            "Loan Application Triage",
            [
                "Intake API: application + document checklist validation",
                "Triage engine: Step Functions state machine; rule-based banding from configuration",
                "Summary Lambda: Bedrock drafts the credit memo summary (advisory, cited fields)",
                "Approval API: maker-checker with segregation-of-duties enforcement",
            ],
            ["Application intake", "Completeness checks", "Policy rule banding", "Advisory summary draft", "Maker-checker decision"],
        ),
        "tasks": _tasks(
            "Loan Application Triage",
            [
                "Provision Step Functions, DynamoDB application table, IAM roles",
                "Intake API with checklist validation + document references",
                "Banding rules engine from configuration (no model in the decision path)",
                "Bedrock summary Lambda producing cited advisory memos",
                "Maker-checker approval endpoints with segregation-of-duties checks + state history export",
            ],
        ),
    },
    {
        "title": "Fraud Alert Triage Console",
        "description": "Enriches transaction fraud alerts with context and drafts disposition rationales for analyst review.",
        "category": "data_analysis",
        "complexity": "advanced",
        "models_used": [SONNET, HAIKU],
        "keywords": ["fraud", "alerts", "triage", "investigations", "case-management", "financial-services"],
        "metadata_extra": {
            "est_build_usd": 3.0, "est_run_usd_month": 110,
            "tech_stack": ["Lambda", "DynamoDB", "EventBridge", "API Gateway", "Bedrock"],
        },
        "long_description": (
            "A working console for transaction-fraud alert queues: each alert is "
            "enriched with account context and recent activity, grouped into cases, "
            "and given a DRAFT disposition rationale the analyst confirms, edits, or "
            "rejects. The model never closes an alert — it writes the first draft of "
            "the narrative an analyst would otherwise type from scratch. Disposition "
            "stats feed a supervisor view."
        ),
        "requirements": _req(
            "Fraud Alert Triage Console",
            [
                ("fraud analyst", "see alerts enriched with account context and history", "I stop pivoting between five systems"),
                ("fraud analyst", "get a draft disposition rationale I can edit", "case notes take seconds, not minutes"),
                ("team lead", "see queue aging and disposition breakdowns", "staffing and escalation decisions use real numbers"),
            ],
            [
                "Ingest alerts via EventBridge; group related alerts into cases by account + time window",
                "Enrich each case with recent transaction context from the case store",
                "Draft disposition rationale (fraud / not fraud / needs escalation) with cited signals — analyst confirmation required",
                "Analyst edits and decisions are recorded verbatim alongside the draft for model-vs-final comparison",
                "Supervisor dashboard: queue depth, aging, disposition mix; no customer PII on the dashboard",
            ],
        ),
        "design": _design(
            "Fraud Alert Triage Console",
            [
                "Alert ingestion: EventBridge rule → normalizer Lambda → case grouping",
                "Enrichment Lambda: account context assembly from the case store",
                "Rationale Lambda: Bedrock draft with cited signals (temperature low, structured output)",
                "Console UI: queue, case view with draft, confirm/edit/reject actions; supervisor stats",
            ],
            ["Alert arrives", "Case grouping + enrichment", "Draft rationale with cited signals", "Analyst decision + notes", "Supervisor stats"],
        ),
        "tasks": _tasks(
            "Fraud Alert Triage Console",
            [
                "Provision EventBridge bus, DynamoDB case store, IAM roles",
                "Ingestion + case-grouping Lambda (account/time-window rules)",
                "Enrichment assembly + Bedrock rationale draft with signal citations",
                "Console API + UI: queue, case detail, confirm/edit/reject with audit",
                "Supervisor stats endpoint + dashboard cards, PII-free",
            ],
        ),
    },
    {
        "title": "Regulatory Policy Q&A",
        "description": "RAG assistant over compliance manuals and regulatory circulars with mandatory citations and refusal on low confidence.",
        "category": "chatbot",
        "complexity": "beginner",
        "models_used": [SONNET, HAIKU],
        "keywords": ["compliance", "regulatory", "rag", "citations", "policies", "financial-services"],
        "metadata_extra": {
            "est_build_usd": 1.5, "est_run_usd_month": 50,
            "tech_stack": ["Lambda", "OpenSearch Serverless", "S3", "Bedrock"],
        },
        "long_description": (
            "A citation-first Q&A assistant for compliance teams: internal manuals, "
            "procedures, and regulatory circulars are indexed from S3, and every "
            "answer cites document, section, and effective date. When retrieval "
            "confidence is low the assistant says so and points to the compliance "
            "contact instead of guessing — the refusal behavior is a feature, not a "
            "limitation.\n\n**Good first fork if** your first-line teams ping "
            "compliance for answers that are already written down."
        ),
        "requirements": _req(
            "Regulatory Policy Q&A",
            [
                ("relationship manager", "ask policy questions in plain language", "I get answers without waiting on compliance"),
                ("compliance officer", "see which documents answer which questions", "I find gaps in our manuals"),
                ("compliance officer", "update the corpus when circulars change", "answers always reflect current guidance"),
            ],
            [
                "Index PDF/Markdown manuals and circulars from S3 with document effective dates",
                "Answers cite document title, section, and effective date on every response",
                "Refuse with a compliance-contact pointer when retrieval confidence is low",
                "Track unanswered questions for corpus gap review",
                "Re-index on demand; stale-document warnings when effective dates lapse",
            ],
        ),
        "design": _design(
            "Regulatory Policy Q&A",
            [
                "Ingestion Lambda: chunk + embed with effective-date metadata",
                "Query API: retrieval with date-aware ranking; Bedrock synthesis grounded to chunks",
                "Refusal gate: confidence threshold with compliance-contact fallback",
                "Gap log: unanswered questions table for corpus review",
            ],
            ["Question", "Date-aware retrieval", "Grounded synthesis with citations", "Answer or explicit refusal"],
        ),
        "tasks": _tasks(
            "Regulatory Policy Q&A",
            [
                "Provision S3, OpenSearch Serverless collection, IAM roles",
                "Ingestion Lambda with effective-date metadata + stale warnings",
                "Query Lambda: retrieval, confidence gate, grounded synthesis with citations",
                "Chat UI with citation cards + refusal messaging",
                "Gap-log endpoint + re-index action + smoke tests",
            ],
        ),
    },
    {
        "title": "Portfolio Performance Reporter",
        "description": "Natural-language questions over portfolio holdings and transactions with charts and compliance-safe commentary.",
        "category": "data_analysis",
        "complexity": "intermediate",
        "models_used": [SONNET],
        "keywords": ["portfolio", "performance", "reporting", "analytics", "wealth", "financial-services"],
        "metadata_extra": {
            "est_build_usd": 2.0, "est_run_usd_month": 65,
            "tech_stack": ["Lambda", "Athena", "S3", "QuickSight-optional", "Bedrock"],
        },
        "long_description": (
            "Ask questions like \"top contributors to Q2 performance in the balanced "
            "mandates\" against holdings and transaction extracts in S3. Queries are "
            "generated against an approved schema catalog (read-only, allow-listed "
            "tables), results render as tables and charts, and the narrative "
            "commentary carries a standard not-investment-advice disclaimer. "
            "Numbers come from the query engine — the model writes words, not sums."
        ),
        "requirements": _req(
            "Portfolio Performance Reporter",
            [
                ("portfolio manager", "ask performance questions in natural language", "I skip writing SQL for routine cuts"),
                ("investment counsellor", "export a client-ready summary with charts", "review prep takes minutes"),
                ("compliance officer", "know every generated query is read-only and logged", "the tool cannot mutate or exfiltrate data"),
            ],
            [
                "Generate queries only against an allow-listed, read-only schema catalog",
                "All aggregation is computed by the query engine; model narrative must reference computed figures only",
                "Charts (time series, contribution bars) render from result sets",
                "Every generated query is logged verbatim with the requesting user",
                "Narrative output carries the standard informational-only disclaimer",
            ],
        ),
        "design": _design(
            "Portfolio Performance Reporter",
            [
                "Schema catalog: allow-listed Athena views over S3 extracts",
                "Query Lambda: NL → SQL against the catalog, read-only enforcement, verbatim logging",
                "Chart service: result-set to chart config (no model in the numbers path)",
                "Narrative Lambda: Bedrock commentary grounded to computed figures + disclaimer",
            ],
            ["NL question", "Catalog-constrained SQL", "Engine computes results", "Charts + grounded narrative"],
        ),
        "tasks": _tasks(
            "Portfolio Performance Reporter",
            [
                "Provision Athena workgroup, S3 extracts layout, allow-listed views, IAM roles",
                "NL→SQL Lambda with catalog constraints + read-only guard + query log",
                "Chart config generator from result sets",
                "Narrative Lambda grounded to computed figures with disclaimer",
                "Export endpoint (summary + charts) + smoke tests",
            ],
        ),
    },
    {
        "title": "Client Meeting Note Summarizer",
        "description": "Turns advisor call notes into CRM-ready summaries with action items, disclosures checklist, and review before save.",
        "category": "content_generation",
        "complexity": "beginner",
        "models_used": [SONNET, HAIKU],
        "keywords": ["advisory", "meeting-notes", "crm", "suitability", "summaries", "financial-services"],
        "metadata_extra": {
            "est_build_usd": 1.0, "est_run_usd_month": 35,
            "tech_stack": ["Lambda", "API Gateway", "DynamoDB", "Bedrock"],
        },
        "long_description": (
            "Paste raw meeting notes (or upload a transcript) and get a structured "
            "summary: discussion points, client objectives mentioned, action items "
            "with owners, and a disclosures checklist (risk warnings given, products "
            "discussed). Nothing saves to the record until the advisor reviews and "
            "confirms — the draft is clearly watermarked until then."
        ),
        "requirements": _req(
            "Client Meeting Note Summarizer",
            [
                ("advisor", "turn raw notes into a structured summary", "post-meeting admin takes two minutes"),
                ("advisor", "review and edit before anything is saved", "the record stays accurate and mine"),
                ("supervisor", "see disclosures checklists across meetings", "suitability reviews sample efficiently"),
            ],
            [
                "Summarize notes into discussion points, objectives, action items with owners and dates",
                "Produce a disclosures checklist from the notes (risk warnings, products discussed) — advisor confirms each item",
                "Drafts are watermarked and unsaved until advisor confirmation; edits recorded",
                "Client identifiers masked in logs; summaries stored encrypted",
                "Supervisor view lists confirmed checklists only",
            ],
        ),
        "design": _design(
            "Client Meeting Note Summarizer",
            [
                "Summary Lambda: Bedrock structured output (sections, action items, checklist)",
                "Review API: draft state, edit tracking, confirm-to-save",
                "Record store: DynamoDB encrypted summaries with confirmation metadata",
                "Supervisor view: confirmed checklists index",
            ],
            ["Notes input", "Structured draft with checklist", "Advisor review + edits", "Confirmed save to record"],
        ),
        "tasks": _tasks(
            "Client Meeting Note Summarizer",
            [
                "Provision DynamoDB record store, API Gateway, IAM roles",
                "Bedrock structured summary Lambda (sections, actions, disclosures checklist)",
                "Draft/review/confirm API with edit tracking + watermark states",
                "Supervisor confirmed-checklist index endpoint",
                "UI: paste/upload, draft review pane, confirm flow + smoke tests",
            ],
        ),
    },
    # ---- FS pack extension (5 Aug 2026): four more samples for
    # live-demo depth. Same rules as above: institution-neutral, governance-
    # leaning, human-in-the-loop wherever a decision could bind the firm.
    {
        "title": "Suitability Check Assistant",
        "description": "Drafts suitability assessments for advised product sales, citing the client profile facts each conclusion rests on.",
        "category": "workflow_automation",
        "complexity": "advanced",
        "models_used": [SONNET],
        "keywords": ["suitability", "advice", "mifid", "conduct", "financial-services", "human-in-the-loop"],
        "metadata_extra": {
            "est_build_usd": 3.0, "est_run_usd_month": 120,
            "tech_stack": ["Lambda", "API Gateway", "DynamoDB", "Bedrock"],
        },
        "long_description": (
            "Given a client profile (objectives, horizon, knowledge, capacity for loss) "
            "and a proposed product, drafts the suitability assessment an adviser must "
            "complete — with every conclusion linked to the profile fact it rests on, "
            "and an explicit UNSUITABLE-UNLESS list where facts are missing. The draft "
            "is always a draft: the adviser confirms or amends each section, and the "
            "record keeps both the draft and the human's final wording.\n\n"
            "**Good first fork if** advisers spend more time writing assessments than "
            "having client conversations."
        ),
        "requirements": _req(
            "Suitability Check Assistant",
            [
                ("financial adviser", "get a drafted assessment from the client profile", "I spend the meeting advising, not transcribing"),
                ("compliance officer", "see which profile fact supports each conclusion", "file reviews take minutes, not hours"),
                ("adviser", "be told what CANNOT be concluded from the profile", "gaps surface before the recommendation, not after"),
            ],
            [
                "Draft each assessment section (objectives fit, risk alignment, costs, alternatives) from the structured client profile",
                "Every drafted conclusion cites the profile field(s) it rests on; unsupported conclusions are refused",
                "Missing-fact detection: emit an explicit list of facts required before a conclusion is possible",
                "Adviser confirmation is mandatory; the record stores draft and final wording side by side",
                "Assessments are exportable for file review with the full provenance trail",
            ],
        ),
        "design": _design(
            "Suitability Check Assistant",
            [
                "Profile API: structured client profile CRUD (DynamoDB), validation of required fields",
                "Drafting Lambda: per-section Bedrock drafting with fact-citation constraints",
                "Gap checker: deterministic required-facts matrix per product class",
                "Review API: section-level confirm/amend with dual-record storage",
            ],
            ["Profile captured", "Sections drafted with citations", "Gaps listed", "Adviser confirms/amends", "Export for file review"],
        ),
        "tasks": _tasks(
            "Suitability Check Assistant",
            [
                "DynamoDB profile + assessment tables, API Gateway + Lambda scaffold",
                "Required-facts matrix per product class (deterministic, code not model)",
                "Drafting Lambda with per-section citation constraints",
                "Confirm/amend endpoints storing draft and final wording",
                "File-review export + smoke tests",
            ],
        ),
    },
    {
        "title": "Trade Surveillance Case Notes",
        "description": "Summarizes market-abuse alert cases — orders, executions, comms references — into review-ready narratives with an evidence index.",
        "category": "data_analysis",
        "complexity": "advanced",
        "models_used": [SONNET, HAIKU],
        "keywords": ["surveillance", "market-abuse", "alerts", "case-management", "financial-services"],
        "metadata_extra": {
            "est_build_usd": 3.0, "est_run_usd_month": 150,
            "tech_stack": ["Lambda", "API Gateway", "DynamoDB", "S3", "Bedrock"],
        },
        "long_description": (
            "Surveillance analysts inherit alert cases as piles of order events, "
            "executions and comms references. This sample assembles the pile into a "
            "chronological narrative — what happened, in what sequence, involving whom — "
            "with every narrative sentence indexed back to the underlying records. It "
            "deliberately does NOT score or classify the alert: deciding whether "
            "behaviour is abusive stays with the analyst; the sample removes the "
            "assembly work, not the judgement.\n\n"
            "**Good first fork if** your analysts spend the first hour of every case "
            "building the timeline by hand."
        ),
        "requirements": _req(
            "Trade Surveillance Case Notes",
            [
                ("surveillance analyst", "get a chronological case narrative from the raw events", "I start at the judgement, not the assembly"),
                ("team lead", "see every narrative claim linked to its source records", "QA of closed cases is evidence-based"),
                ("analyst", "append my own findings to the case record", "the final note is mine, with the machine's draft attached"),
            ],
            [
                "Ingest order/execution/comms-reference events for a case id; normalize to a common timeline schema",
                "Generate a chronological narrative; every sentence carries an evidence index into the source records",
                "No classification or scoring of the alert — narrative assembly only, judgement stays human",
                "Analyst findings append to the case; draft narrative and final disposition are both retained",
                "Case export bundles narrative, evidence index and raw records for regulators or QA",
            ],
        ),
        "design": _design(
            "Trade Surveillance Case Notes",
            [
                "Ingest API: event batches per case into DynamoDB, S3 for raw payloads",
                "Timeline builder: deterministic ordering + entity resolution in code",
                "Narrative Lambda: Bedrock generation constrained to indexed sentences",
                "Case API: findings append, disposition record, export bundle",
            ],
            ["Events ingested", "Timeline built deterministically", "Narrative drafted with evidence index", "Analyst disposition", "Export"],
        ),
        "tasks": _tasks(
            "Trade Surveillance Case Notes",
            [
                "Case/event tables + ingest API with schema validation",
                "Deterministic timeline builder with entity resolution",
                "Narrative generation with per-sentence evidence indexing",
                "Findings + disposition endpoints, retention of draft vs final",
                "Export bundle (narrative + index + raw) and smoke tests",
            ],
        ),
    },
    {
        "title": "Regulatory Change Digest",
        "description": "Turns regulator publications into a weekly obligations digest mapped to your policy inventory, with owners and due dates.",
        "category": "content_generation",
        "complexity": "intermediate",
        "models_used": [SONNET, HAIKU],
        "keywords": ["regulatory-change", "horizon-scanning", "obligations", "policy", "financial-services"],
        "metadata_extra": {
            "est_build_usd": 2.0, "est_run_usd_month": 60,
            "tech_stack": ["Lambda", "EventBridge", "DynamoDB", "S3", "Bedrock"],
        },
        "long_description": (
            "Each week, ingests the regulator publications you point it at, extracts "
            "candidate obligations ('firms must…'), and maps each against your policy "
            "inventory: already covered, needs amendment, or new. The output is a "
            "digest a compliance manager can triage in one sitting — accept a mapping "
            "and it lands on the obligations register with an owner and a due date; "
            "reject it and the correction trains nothing silently (it is simply "
            "recorded). Mapping suggestions are suggestions: the register only ever "
            "changes by human acceptance.\n\n"
            "**Good first fork if** horizon scanning is a folder of PDFs and a "
            "spreadsheet."
        ),
        "requirements": _req(
            "Regulatory Change Digest",
            [
                ("compliance manager", "receive a weekly digest of extracted obligations", "nothing published slips past the team"),
                ("policy owner", "see which policy each obligation maps to and why", "amendments start from the right document"),
                ("head of compliance", "track acceptance and due dates on a register", "progress against regulatory change is evidenced"),
            ],
            [
                "Scheduled ingest of configured publication sources; store originals immutably",
                "Extract candidate obligations with the exact source passage quoted alongside",
                "Map candidates to the policy inventory with a stated rationale; propose covered/amend/new",
                "Register updates ONLY on human acceptance; rejections are recorded with reasons",
                "Weekly digest with per-item links; register export with owners and due dates",
            ],
        ),
        "design": _design(
            "Regulatory Change Digest",
            [
                "Scheduled ingest Lambda (EventBridge): fetch, checksum, store to S3",
                "Extraction Lambda: obligation candidates with quoted passages",
                "Mapping Lambda: policy-inventory comparison with rationale",
                "Register API: accept/reject, owners, due dates, digest assembly",
            ],
            ["Weekly ingest", "Obligations extracted with quotes", "Mapped to policies", "Human accepts/rejects", "Register + digest"],
        ),
        "tasks": _tasks(
            "Regulatory Change Digest",
            [
                "Source config + scheduled ingest with immutable storage",
                "Obligation extraction with source-passage quoting",
                "Policy-inventory mapping with rationale output",
                "Register API: accept/reject flow, owners, due dates",
                "Digest assembly + export, smoke tests",
            ],
        ),
    },
    {
        "title": "Credit Memo Drafter",
        "description": "Drafts commercial credit memos from financial spreads and covenant data, with every figure traced to its source cell.",
        "category": "content_generation",
        "complexity": "intermediate",
        "models_used": [SONNET],
        "keywords": ["credit", "commercial-lending", "memo", "underwriting", "financial-services"],
        "metadata_extra": {
            "est_build_usd": 2.5, "est_run_usd_month": 90,
            "tech_stack": ["Lambda", "API Gateway", "DynamoDB", "Bedrock"],
        },
        "long_description": (
            "Takes the structured inputs an underwriter already has — financial "
            "spreads, covenant schedule, facility request — and drafts the narrative "
            "sections of the credit memo: business overview, financial analysis, "
            "repayment capacity, risks and mitigants. Every figure in the draft is "
            "traced to its source field, so checking the memo is reading, not "
            "re-deriving. The recommendation section is deliberately left to the "
            "underwriter: the sample drafts analysis, never the decision.\n\n"
            "**Good first fork if** memo-writing is the bottleneck between analysis "
            "done and committee-ready."
        ),
        "requirements": _req(
            "Credit Memo Drafter",
            [
                ("underwriter", "get narrative sections drafted from my spreads", "committee packs assemble in hours, not days"),
                ("credit officer", "trace every figure in the memo to its source field", "review is verification, not re-derivation"),
                ("underwriter", "own the recommendation section entirely", "the machine drafts analysis, I make the call"),
            ],
            [
                "Accept structured spreads, covenant schedules and facility requests via API",
                "Draft business overview, financial analysis, repayment capacity, risks and mitigants sections",
                "Every figure carries a source reference to the input field it came from",
                "The recommendation section is never generated — underwriter-authored only",
                "Memo versions are retained; the committee export marks drafted vs human-authored sections",
            ],
        ),
        "design": _design(
            "Credit Memo Drafter",
            [
                "Intake API: spreads/covenants/request schemas with validation (DynamoDB)",
                "Ratio engine: deterministic computation of the standard ratio set in code",
                "Drafting Lambda: section-by-section Bedrock drafting with figure tracing",
                "Memo API: versioned sections, authorship flags, committee export",
            ],
            ["Inputs captured", "Ratios computed in code", "Sections drafted with figure tracing", "Underwriter writes recommendation", "Committee export"],
        ),
        "tasks": _tasks(
            "Credit Memo Drafter",
            [
                "Input schemas + intake API with validation",
                "Deterministic ratio engine (code, not model)",
                "Section drafting with per-figure source references",
                "Versioned memo store with authorship flags",
                "Committee export + smoke tests",
            ],
        ),
    },
]
