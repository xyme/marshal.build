"""Codegen-quality sprint (FSD B7/B8): spec-conformance report + inline-ceiling
auto-retry. The extraction fixtures include the two 5 Aug 2026 live
reproductions (band drift, invented field) per R1.7 — the tests prove what the
shipped checks actually catch, no overclaim."""

import asyncio
import json
import uuid

import pytest

from app.models import CodegenBuild, Project, Spec, User
from app.services.codegen import conformance as conf
from app.services.codegen import runner
from app.services.codegen.conformance import (
    conformance_report,
    deterministic_verdicts,
    extract_contracts,
    review_criteria,
)
from app.services.codegen.literals import is_negated_criterion
from app.services.codegen.provider import BuildCtx, BuildPlan, PlannedFile
from app.services.codegen.validate import MAX_INLINE_CHARS, validate_artifacts

pytestmark = pytest.mark.asyncio

# --------------------------------------------------------------- fixtures

BAND_CRITERION = (
    '- WHEN an application is scored THE SYSTEM SHALL return prequalification_band '
    'as one of "likely", "possible", "refer"'
)
FIELD_CRITERION = (
    "- The request SHALL accept exactly {applicant_ref, annual_income, loan_amount} "
    "and no other fields"
)
REQUIREMENTS_MD = f"""# Requirements

## Acceptance criteria

{BAND_CRITERION}
{FIELD_CRITERION}
- The endpoint SHALL respond within a reasonable time budget.

Endpoint authentication: PUBLIC

Prose paragraph quoting "the code matches the spec" must not become a contract.
"""

# The 5 Aug band-drift reproduction: generated code shipped `conditional` /
# `denied` alongside `likely`; `possible` and `refer` never appear.
DRIFTED_SOURCE = """\
def handler(event, context):
    band = "likely"
    if score < 40:
        band = "denied"
    elif score < 60:
        band = "conditional"
    body = {"applicant_ref": ref, "annual_income": income, "loan_amount": amount}
    return band
"""

CONFORMING_SOURCE = """\
def handler(event, context):
    band = "likely" if score > 70 else ("possible" if score > 50 else "refer")
    body = {"applicant_ref": ref, "annual_income": income, "loan_amount": amount}
    return band
"""


# ------------------------------------------------------ extraction (R1.2)


def test_extract_contracts_enum_and_fields():
    contracts = extract_contracts(REQUIREMENTS_MD)
    kinds = {c.kind: c for c in contracts}
    assert set(kinds) == {"enum_literals", "field_contract"}
    assert kinds["enum_literals"].items == ("likely", "possible", "refer")
    assert kinds["field_contract"].items == (
        "applicant_ref", "annual_income", "loan_amount",
    )
    # criterion line rides along as the evidence anchor
    assert "prequalification_band" in kinds["enum_literals"].criterion


def test_extraction_is_conservative():
    # Prose quotations (spaces), single literals, and non-criterion lines
    # must extract NOTHING — ambiguity goes to the model review instead.
    text = (
        'The band "likely" appears alone here.\n'
        '- A quoted phrase "the code matches the spec" is prose.\n'
        "Plain sentence with {braces, list} but no normative marker.\n"
    )
    # the brace list line has no bullet/SHALL marker… but starts with "Plain"
    # (no marker) — and the bulleted line has only multi-word phrases.
    assert extract_contracts(text) == []


def test_band_drift_reproduction_is_caught():
    """R1.7: the live band-drift finding — deterministic pass alone."""
    contracts = extract_contracts(REQUIREMENTS_MD)
    verdicts = deterministic_verdicts(
        contracts, {"src/handler.py": DRIFTED_SOURCE, "README.md": "# x"}
    )
    enum_verdicts = [v for v in verdicts if v["check"] == "enum_literals"]
    assert len(enum_verdicts) == 1 and enum_verdicts[0]["verdict"] == "violated"
    assert "'possible'" in enum_verdicts[0]["evidence"]
    assert "'refer'" in enum_verdicts[0]["evidence"]
    # fields are all present in the drifted source — that contract is met
    field_verdicts = [v for v in verdicts if v["check"] == "field_contract"]
    assert field_verdicts[0]["verdict"] == "met"


def test_conforming_source_is_met():
    contracts = extract_contracts(REQUIREMENTS_MD)
    verdicts = deterministic_verdicts(contracts, {"src/handler.py": CONFORMING_SOURCE})
    assert {v["verdict"] for v in verdicts} == {"met"}


# ------------------------------- non-normative text is not a contract (hotfix)
# Live false positive: a generated requirements document always ends with a
# mandatory "## Assumptions" section; one bullet there quoted `needs-manager`,
# `policy-violation` and "notifications", the extractor turned that into a
# blocking enum_literals contract, and a correct build failed the gate.

PRODUCTION_ASSUMPTION_BULLET = (
    "- **(ASSUMPTION)** No notification mechanism (email/SNS) is triggered when a "
    "receipt is classified as `needs-manager` or `policy-violation`; the template "
    'calls for "notifications" generally.'
)
OUT_OF_SCOPE_BULLET = (
    "- Manager approval action is out of scope (`pending-manager-review` state); an "
    "actual manager sign-off workflow (transition to `approved`/`rejected`) is NOT "
    "implemented."
)
TRIAGE_REQUIREMENTS_MD = f"""# Requirements

## Functional Requirements

### FR-1: Receipt triage

- WHEN a receipt is submitted THE SYSTEM SHALL classify it as one of `approved`, \
`needs-manager`, `policy-violation`.
- WHEN lookup fails THEN the system SHALL return status "not-found" or "expired".
- The request SHALL accept exactly {{receipt_id, amount}}.

## Assumptions

{PRODUCTION_ASSUMPTION_BULLET}

## Constraints

- Responses SHALL use codes "ok", "error".

## Out of Scope

{OUT_OF_SCOPE_BULLET}
"""
TRIAGE_SOURCE = """\
def handler(event, context):
    body = {"receipt_id": event["receipt_id"], "amount": event["amount"]}
    verdict = "approved" if body["amount"] < 50 else "needs-manager"
    if body["receipt_id"] is None:
        return {"code": "error", "status": "not-found"}
    if verdict == "policy-violation" or expired(body):
        return {"code": "error", "status": "expired"}
    return {"code": "ok", "verdict": verdict}
"""

POSITIVE_ENUM_LINE = '- The status SHALL be one of "open", "closed".\n'


def _items(contracts) -> set[tuple[str, tuple[str, ...]]]:
    return {(c.kind, c.items) for c in contracts}


def test_production_assumption_bullet_no_longer_becomes_a_contract():
    contracts = extract_contracts(TRIAGE_REQUIREMENTS_MD)
    assert _items(contracts) == {
        ("enum_literals", ("approved", "needs-manager", "policy-violation")),
        ("enum_literals", ("not-found", "expired")),
        ("field_contract", ("receipt_id", "amount")),
        ("enum_literals", ("ok", "error")),  # the section AFTER Assumptions resumed
    }
    quoted_in_non_normative_text = {"notifications", "rejected", "pending-manager-review"}
    assert not quoted_in_non_normative_text & {i for c in contracts for i in c.items}
    # A correct build now passes the gate; the reporting shape is untouched.
    verdicts = deterministic_verdicts(contracts, {"src/triage.py": TRIAGE_SOURCE})
    assert len(verdicts) == 4
    assert {v["verdict"] for v in verdicts} == {"met"}
    assert all(
        set(v) == {"source", "check", "verdict", "criterion", "evidence"} for v in verdicts
    )


def test_out_of_scope_section_quoting_states_yields_nothing():
    assert extract_contracts(f"## Out of scope\n\n{OUT_OF_SCOPE_BULLET}\n") == []
    assert extract_contracts(f"### Out-of-Scope\n{OUT_OF_SCOPE_BULLET}\n") == []


@pytest.mark.parametrize(
    "heading",
    [
        "Assumptions", "Assumption", "Stated Assumptions", "Assumptions & Constraints",
        "Out of scope", "Out-of-Scope", "Out of Scope (v1)", "Non-goals", "Non Goals",
        "Not in scope", "Exclusions", "Limitations", "Known Limitations", "Open Questions",
        "Future Work", "Future Considerations", "Future Enhancements", "Deferred",
        "Deferred Items", "Risks", "Risks and Mitigations", "Known Risks",
    ],
)
def test_non_normative_headings_hide_contracts(heading):
    assert extract_contracts(f"## {heading}\n{POSITIVE_ENUM_LINE}") == []
    assert extract_contracts(f"#### {heading}\n{POSITIVE_ENUM_LINE}") == []


@pytest.mark.parametrize(
    "heading",
    [
        "Functional Requirements", "Acceptance criteria", "Risk Scoring",
        "FR-3: Risk Assessment Workflow", "Constraints", "Non-Functional Requirements",
        "Notifications", "Rate Limiting", "Scope",
        # FR-n headings are normative by construction even when the feature
        # name carries a vocabulary word.
        "FR-4: Deferred Settlement", "FR-2: Risks Summary", "FR-7: Exclusions Handling",
        "NFR-1: Known Limitations", "**FR-5: Open Questions Queue**",
    ],
)
def test_normative_headings_keep_extracting(heading):
    for level in ("##", "####"):
        contracts = extract_contracts(f"{level} {heading}\n{POSITIVE_ENUM_LINE}")
        assert _items(contracts) == {("enum_literals", ("open", "closed"))}


@pytest.mark.parametrize(
    "app_name",
    [
        "Vendor Risks Dashboard", "Deferred Revenue Tracker", "Policy Exclusions Checker",
        "Assumptions Log", "Non-goals Tracker", "Open Questions Board",
        "Future Work Planner", "Known Limitations Register", "Out of Scope Items",
    ],
)
def test_document_title_with_a_vocabulary_word_does_not_hide_the_body(app_name):
    # The skeleton's only level-1 heading is the title "# Requirements — <App
    # Name>", and nothing deeper could end a level-1 skip: the whole document
    # would yield no contracts and the gate would be silently off.
    text = (
        f"# Requirements — {app_name}\n\n"
        "## Introduction\n"
        f"{app_name} scores applications.\n\n"
        "## Functional Requirements\n"
        "### FR-1: Scoring\n"
        f"{BAND_CRITERION}\n"
        f"{FIELD_CRITERION}\n\n"
        "## Assumptions\n"
        '- The status is assumed to be "draft" or "final".\n'  # still skipped
    )
    assert _items(extract_contracts(text)) == {
        ("enum_literals", ("likely", "possible", "refer")),
        ("field_contract", ("applicant_ref", "annual_income", "loan_amount")),
    }


def test_level_one_heading_is_a_title_and_never_starts_a_skip():
    # Pins the trade-off: a bare "# Assumptions" extracts as it did before the
    # hotfix (the skip vocabulary applies from level 2 down), and a level-1
    # heading always ends a deeper skipped section.
    assert _items(extract_contracts(f"# Assumptions\n{POSITIVE_ENUM_LINE}")) == {
        ("enum_literals", ("open", "closed"))
    }
    text = (
        "## Out of scope\n"
        '- The export format SHALL be "csv" or "pdf".\n'
        "# Risks Register\n"
        f"{POSITIVE_ENUM_LINE}"
    )
    assert _items(extract_contracts(text)) == {("enum_literals", ("open", "closed"))}


def test_negated_bullets_under_a_normative_heading_yield_nothing():
    text = (
        "## Functional Requirements\n"
        '- No notification is sent for "needs-manager" or "policy-violation".\n'
        '- The workflow does not transition to "approved" or "rejected".\n'
    )
    assert extract_contracts(text) == []


def test_skipped_section_resumes_only_at_same_or_shallower_heading():
    text = (
        "## Assumptions\n"
        '- The status SHALL be one of "x1", "y1".\n'
        "### Details\n"  # deeper heading: still inside Assumptions
        '- The status SHALL be one of "p1", "q1".\n'
        "## Constraints\n"  # same level: extraction resumes
        '- Responses SHALL use codes "ok", "error".\n'
        "# Top\n"  # shallower level after a normative section: still extracting
        '- The mode SHALL be "m1" or "n1".\n'
    )
    assert [c.items for c in extract_contracts(text)] == [("ok", "error"), ("m1", "n1")]


@pytest.mark.parametrize(
    "line",
    [
        '- No notification is sent for "alpha" or "beta".',
        '- Not applicable to "alpha"/"beta".',
        '- Never returns "alpha" or "beta".',
        '- Without "alpha" or "beta" support.',
        '- The service does not emit "alpha" or "beta".',
        '- Clients do not send "alpha" or "beta".',
        '- The system will not expose "alpha" or "beta".',
        '- The system won\'t expose "alpha" or "beta".',
        '- The system won\u2019t expose "alpha" or "beta".',
        '- The API should not return "alpha" or "beta".',
        '- The field is not one of "alpha", "beta".',
        '- States "alpha" and "beta" are not persisted.',
        '- "alpha"/"beta" handling is not required.',
        '- The response does not include "alpha" or "beta".',
        '- Audit of "alpha"/"beta" is not in scope.',
        '- "alpha"/"beta" transitions are out of scope.',
        '- "alpha" and "beta" are excluded.',
        "- The API will not accept {ssn, dob}.",
        "- {ssn, dob} is not required.",
        '1. No "alpha" or "beta" values are stored.',
        '- [ ] No "alpha" or "beta" values are stored.',
        # inline tag outside any section (the production bullet's shape)
        '- **(ASSUMPTION)** No notification mechanism is triggered for "alpha" or "beta".',
        '- **(Out of scope)** Exporting "csv" or "pdf".',
        # the pre-existing SHALL NOT / MUST NOT skip still holds, mid-sentence
        # and inside a leading tag (the tag is stripped before the prose test,
        # so it is judged on its own)
        "- The API MUST NOT accept {ssn, date_of_birth}.",
        '- The result SHALL NOT return one of "secret", "credential".',
        "- (MUST NOT) accept {ssn, dob}.",
        "- [SHALL NOT be logged] {ssn, dob}.",
        "- **(MUST NOT)** accept {ssn, dob}.",
        '- (Not required) Support for "alpha" or "beta".',
    ],
)
def test_negated_prose_lines_are_skipped(line):
    assert extract_contracts(line) == []
    assert is_negated_criterion(line) is True


def test_negated_when_condition_drops_its_positive_consequent():
    # Documented trade-off, not desired behaviour: the in-sentence negation
    # test is unanchored, so a negated WHEN/IF condition hides a positive
    # SHALL consequent (fail-open: the gate checks less, never fails a correct
    # build). Follow-up: judge negation on the clause after THEN only. This
    # pin exists so the next vocabulary change notices if the drop changes.
    line = (
        '- WHEN the user is not authenticated THEN the system SHALL return '
        '"unauthorized" or "forbidden".'
    )
    assert extract_contracts(line) == []
    assert is_negated_criterion(line) is True


@pytest.mark.parametrize(
    ("line", "kind", "items"),
    [
        ('- The band WILL be one of "likely", "possible"', "enum_literals", ("likely", "possible")),
        ('- The status should be "open" or "closed"', "enum_literals", ("open", "closed")),
        ('- **Status values:** "ready", "failed"', "enum_literals", ("ready", "failed")),
        ('- Notifications SHALL use channels "email", "sns".', "enum_literals", ("email", "sns")),
        ('- Nothing else: the states SHALL be "open", "closed".', "enum_literals", ("open", "closed")),
        ('- The No-code builder SHALL expose "draft", "published"', "enum_literals", ("draft", "published")),
        ('- Non-admin users SHALL see "read", "comment".', "enum_literals", ("read", "comment")),
        ('- [x] The status SHALL be "open" or "closed".', "enum_literals", ("open", "closed")),
        (BAND_CRITERION, "enum_literals", ("likely", "possible", "refer")),
        (FIELD_CRITERION, "field_contract", ("applicant_ref", "annual_income", "loan_amount")),
    ],
)
def test_positive_normative_lines_extract_exactly_as_today(line, kind, items):
    contracts = extract_contracts(line)
    assert [(c.kind, c.items) for c in contracts] == [(kind, items)]
    assert contracts[0].criterion == line.strip()  # criterion clip unchanged
    assert is_negated_criterion(line) is False


def test_module_fixture_extraction_unchanged():
    # The pre-existing document (no non-normative sections, no negations)
    # yields exactly what test_extract_contracts_enum_and_fields pins.
    assert _items(extract_contracts(REQUIREMENTS_MD)) == {
        ("enum_literals", ("likely", "possible", "refer")),
        ("field_contract", ("applicant_ref", "annual_income", "loan_amount")),
    }


@pytest.mark.parametrize(
    ("line", "kind", "items"),
    [
        (
            '- WHEN lookup fails THEN the system SHALL return status "not-found" or "expired".',
            "enum_literals", ("not-found", "expired"),
        ),
        ('- The flag SHALL be one of "included", "excluded".', "enum_literals", ("included", "excluded")),
        (
            "- The request SHALL accept exactly {not_before, not_after}.",
            "field_contract", ("not_before", "not_after"),
        ),
        ('- "not-found" and "expired" SHALL be the only error codes.', "enum_literals", ("not-found", "expired")),
    ],
)
def test_negation_words_inside_literals_do_not_negate(line, kind, items):
    # Negation is judged on the prose with quoted literals / brace lists
    # masked, so `not-found`, `excluded` or `{not_before, ...}` never negate
    # their own line — even when the literal is the first word.
    assert [(c.kind, c.items) for c in extract_contracts(line)] == [(kind, items)]
    assert is_negated_criterion(line) is False


# ---------------------------------------------------- model review (R1.3)

REVIEW_JSON = json.dumps(
    {
        "criteria": [
            {
                "criterion": "No PII fields beyond the declared request contract",
                "verdict": "violated",
                "evidence": 'handler reads body["full_name"] — an invented PII field',
            },
            {
                "criterion": "Responds via API Gateway proxy contract",
                "verdict": "met",
                "evidence": 'returns {"statusCode": 200, ...}',
            },
        ]
    }
)


async def test_review_parses_and_retries_once(monkeypatch):
    """R1.7 (invented-field half): the review path formats the violation;
    one retry on malformed output (the risk-rubric precedent)."""
    calls = []

    async def fake_converse(**kwargs):
        calls.append(kwargs)
        text = "not json at all" if len(calls) == 1 else REVIEW_JSON
        return text, {"input_tokens": 1, "output_tokens": 1}, "end_turn"

    monkeypatch.setattr(conf, "converse", fake_converse)
    verdicts = await review_criteria(
        REQUIREMENTS_MD,
        {"src/handler.py": DRIFTED_SOURCE},
        model_id="fast-model",
        user_id=None,
        project_id=uuid.uuid4(),
        build_id=uuid.uuid4(),
    )
    assert len(calls) == 2
    assert calls[0]["temperature"] == 0.0
    invented = [v for v in verdicts if "full_name" in v["evidence"]]
    assert invented and invented[0]["verdict"] == "violated"
    assert all(v["source"] == "review" for v in verdicts)


async def test_review_unusable_after_retry_raises(monkeypatch):
    async def fake_converse(**kwargs):
        return "{}", {}, "end_turn"

    monkeypatch.setattr(conf, "converse", fake_converse)
    with pytest.raises(ValueError, match="unparseable after retry"):
        await review_criteria(
            REQUIREMENTS_MD,
            {"src/handler.py": DRIFTED_SOURCE},
            model_id="fast-model",
            user_id=None,
            project_id=uuid.uuid4(),
            build_id=uuid.uuid4(),
        )


# ------------------------------------------------- orchestrator (R1.1/R1.4)


async def _build_row(db, user: User, requirements: str) -> CodegenBuild:
    project = Project(user_id=user.id, name="Conf", status="spec_complete")
    db.add(project)
    await db.flush()
    build = CodegenBuild(
        project_id=project.id,
        status="validating",
        created_by=user.id,
        spec_snapshot={"requirements": {"content": requirements, "version": 1}},
    )
    db.add(build)
    await db.commit()
    return build


async def test_report_degrades_to_deterministic(db_session, test_user):
    """Model review down (conftest default stub) → status ok, det verdicts,
    review_error noted — partial beats nothing (R1.4)."""
    build = await _build_row(db_session, test_user, REQUIREMENTS_MD)
    report = await conformance_report(
        db_session, build, {"src/handler.py": DRIFTED_SOURCE}
    )
    assert report["status"] == "ok"
    assert report["review_error"]
    assert report["summary"]["violated"] == 1
    assert all(v["source"] == "deterministic" for v in report["verdicts"])


async def test_report_unavailable_when_nothing_checkable(db_session, test_user):
    build = await _build_row(db_session, test_user, "# Requirements\n\nJust prose.\n")
    report = await conformance_report(db_session, build, {"src/handler.py": "x = 1"})
    assert report["status"] == "unavailable"

    build2 = await _build_row(db_session, test_user, "")
    report2 = await conformance_report(db_session, build2, {})
    assert report2 == {
        "status": "unavailable", "error": "no requirements snapshot on the build"
    }


async def test_report_never_raises(db_session, test_user, monkeypatch):
    build = await _build_row(db_session, test_user, REQUIREMENTS_MD)

    def boom(*_a, **_k):
        raise RuntimeError("extraction exploded")

    monkeypatch.setattr(conf, "extract_contracts", boom)
    report = await conformance_report(db_session, build, {"src/handler.py": "x"})
    assert report["status"] == "unavailable" and "exploded" in report["error"]


# ----------------------------------------------- pipeline landing (R1.1/R1.6)


def _template_embedding(source: str) -> str:
    return json.dumps(
        {
            "Resources": {
                "Fn": {
                    "Type": "AWS::Lambda::Function",
                    "Properties": {
                        "Code": {"ZipFile": source},
                        "Handler": "index.handler",
                        "Runtime": "python3.12",
                        "Role": {"Fn::GetAtt": ["Role", "Arn"]},
                    },
                },
                "Role": {"Type": "AWS::IAM::Role", "Properties": {}},
                "Api": {"Type": "AWS::ApiGateway::RestApi", "Properties": {"Name": "x"}},
            },
            "Outputs": {"ApiUrl": {"Value": "https://example"}},
        }
    )


class QualityProvider:
    """Deterministic provider: emits `sources[n]` for the nth handler
    generation (compaction retries advance n), embeds the CURRENT handler
    source into the template on every assembly."""

    name = "internal"

    def __init__(self, sources: list[str]):
        self.sources = sources
        self.file_calls = 0
        self.compaction_briefs: list[str] = []

    async def plan(self, ctx: BuildCtx, *, profile: str = "inline-cfn") -> BuildPlan:
        return BuildPlan(
            app_name="quality-app",
            architecture_notes="One lambda behind a REST API.",
            files=[
                PlannedFile(path="src/handler.py", brief="the handler"),
                PlannedFile(path="README.md", brief="docs", language="markdown"),
            ],
        )

    async def generate_file(self, ctx, plan, file, generated):
        if file.path == "README.md":
            return "# Demo\n"
        if "COMPACTION RETRY" in file.brief:
            self.compaction_briefs.append(file.brief)
        content = self.sources[min(self.file_calls, len(self.sources) - 1)]
        self.file_calls += 1
        return content

    async def assemble_template(self, ctx, plan, generated):
        return _template_embedding(generated["src/handler.py"])


async def _project_with_reqs(db, owner: User) -> Project:
    project = Project(user_id=owner.id, name="Quality", status="spec_complete")
    db.add(project)
    await db.flush()
    contents = {
        "requirements": REQUIREMENTS_MD,
        "design": "# design\n\nOne lambda.",
        "tasks": "# tasks\n\n- build it",
    }
    for doc_type, content in contents.items():
        db.add(
            Spec(
                project_id=project.id, version=1, type=doc_type,
                content=content, created_by=owner.id,
            )
        )
    await db.commit()
    await db.refresh(project)
    return project


async def _wait_terminal(db, build_id, timeout=5.0) -> CodegenBuild:
    for _ in range(int(timeout * 20)):
        await asyncio.sleep(0.05)
        build = await db.get(CodegenBuild, build_id)
        await db.refresh(build)
        if build.status in runner.TERMINAL_STATES:
            return build
    raise AssertionError("build did not reach a terminal state")


async def test_pipeline_lands_conformance_on_manifest(
    db_session, test_user, client_for, audit_db, monkeypatch
):
    provider = QualityProvider([DRIFTED_SOURCE])
    monkeypatch.setattr(runner, "get_provider", lambda name=None: provider)
    project = await _project_with_reqs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        assert r.status_code == 202, r.text
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
        assert build.status == "failed"
        assert build.error["code"] == "validation_failed"
        report = build.manifest["conformance"]
        assert report["status"] == "ok"
        assert report["summary"]["violated"] >= 1
        assert any(f["check"] == "enum_literals" for f in build.error["findings"])
        assert "inline_retry" not in build.manifest
        # Full report + artifacts stay inspectable on the blocked build.
        r = await client.get(f"/api/v1/projects/{project.id}/builds")
        item = r.json()["items"][0]
        assert item["status"] == "failed"
        assert item["manifest"]["conformance"]["summary"]["violated"] >= 1


# ------------------------------------------------- inline-ceiling retry (R2)

OVERSIZED_SOURCE = (
    "def handler(event, context):\n"
    + "    value = 1  # padding line to overflow the inline ceiling\n" * 80
    + "    return {'statusCode': 200}\n"
)
assert len(OVERSIZED_SOURCE) > MAX_INLINE_CHARS


async def test_ceiling_retry_resolves(db_session, test_user, client_for, audit_db, monkeypatch):
    provider = QualityProvider([OVERSIZED_SOURCE, CONFORMING_SOURCE])
    monkeypatch.setattr(runner, "get_provider", lambda name=None: provider)
    project = await _project_with_reqs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
        assert build.status == "ready", build.error
        assert build.manifest["inline_retry"] == {
            "attempted": True, "files": ["src/handler.py"], "resolved": True,
        }
        # the compaction instruction carried the measured size + ceiling
        assert provider.compaction_briefs and str(MAX_INLINE_CHARS) in provider.compaction_briefs[0]
        # R2.5: artifact rows equal what the template embeds
        r = await client.get(f"/api/v1/builds/{build.id}/artifacts/src/handler.py")
        assert r.json()["content"] == CONFORMING_SOURCE
        template = json.loads(
            (await client.get(f"/api/v1/builds/{build.id}/artifacts/template.json")).json()["content"]
        )
        assert template["Resources"]["Fn"]["Properties"]["Code"]["ZipFile"] == CONFORMING_SOURCE


async def test_ceiling_retry_unresolved_fails_with_actionable_copy(
    db_session, test_user, client_for, audit_db, monkeypatch
):
    provider = QualityProvider([OVERSIZED_SOURCE, OVERSIZED_SOURCE])
    monkeypatch.setattr(runner, "get_provider", lambda name=None: provider)
    project = await _project_with_reqs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
        assert build.status == "failed"
        assert build.error["code"] == "validation_failed"
        assert build.manifest["inline_retry"] == {
            "attempted": True, "files": ["src/handler.py"], "resolved": False,
        }
        messages = [f["message"] for f in build.error["findings"]]
        assert any("inline ceiling" in m and "cdk-app" in m for m in messages)
        # exactly ONE retry: initial generation + one compaction, no loop (R2.1)
        assert provider.file_calls == 2


async def test_non_ceiling_failures_do_not_retry(
    db_session, test_user, client_for, audit_db, monkeypatch
):
    """Mixed findings (allowlist breach) must not trigger compaction."""

    class BadTypeProvider(QualityProvider):
        async def assemble_template(self, ctx, plan, generated):
            doc = json.loads(_template_embedding(generated["src/handler.py"]))
            doc["Resources"]["Rogue"] = {"Type": "AWS::EC2::Instance", "Properties": {}}
            return json.dumps(doc)

    provider = BadTypeProvider([OVERSIZED_SOURCE])
    monkeypatch.setattr(runner, "get_provider", lambda name=None: provider)
    project = await _project_with_reqs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
        assert build.status == "failed"
        assert "inline_retry" not in (build.manifest or {})
        assert provider.file_calls == 1  # no compaction pass


# ------------------------------------------------------- finding copy (R2.4)


def test_size_finding_copy_names_both_ways_out():
    template = _template_embedding(OVERSIZED_SOURCE)
    findings, _ = validate_artifacts(
        {"template.json": template, "src/handler.py": OVERSIZED_SOURCE, "README.md": "# x"},
        [],
    )
    size = [f for f in findings if "inline ceiling" in f.message]
    assert len(size) == 1
    assert "Rebuild" in size[0].message and "cdk-app" in size[0].message
    assert str(len(OVERSIZED_SOURCE)) in size[0].message
    # 13.5Z: the actionable escape hatch on an internal-provider installation
    # is the packaged profile's spec phrase — the copy must teach it verbatim,
    # and must not present cdk-app as unconditionally available.
    assert "SHALL use packaged dependencies" in size[0].message
    assert "workspace-runner" in size[0].message


async def test_model_review_only_violation_remains_advisory(
    db_session, test_user, client_for, audit_db, monkeypatch
):
    """B23 gates deterministic contracts only; model review can be wrong."""
    provider = QualityProvider([CONFORMING_SOURCE])
    monkeypatch.setattr(runner, "get_provider", lambda name=None: provider)

    async def review_only(*args, **kwargs):
        return {
            "status": "ok",
            "summary": {"met": 0, "violated": 1, "unverifiable": 0},
            "verdicts": [
                {
                    "source": "review",
                    "verdict": "violated",
                    "criterion": "subjective criterion",
                    "evidence": "model opinion only",
                }
            ],
        }

    monkeypatch.setattr(runner, "conformance_report", review_only)
    project = await _project_with_reqs(db_session, test_user)
    async with client_for(test_user) as client:
        response = await client.post(f"/api/v1/projects/{project.id}/builds")
        build = await _wait_terminal(db_session, uuid.UUID(response.json()["id"]))
    assert build.status == "ready", build.error
    assert build.manifest["conformance"]["summary"]["violated"] == 1
    assert build.manifest["validation"]["findings"] == []


# ------------------------------------------- size escalation (13.5AA, owner)
# When an inline build still fails on NOTHING but the ZipFile ceiling after
# compaction, the in-process runner re-plans as packaged-cfn with an EMPTY
# manifest. The spec phrase stays the only door to third-party dependencies.


def _packaged_template_for(handler_module: str) -> str:
    return json.dumps({
        "Parameters": {
            "PackageBucket": {"Type": "String"},
            "PackageKey": {"Type": "String"},
        },
        "Resources": {
            "Fn": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Code": {
                        "S3Bucket": {"Ref": "PackageBucket"},
                        "S3Key": {"Ref": "PackageKey"},
                    },
                    "Handler": f"{handler_module}.handler",
                    "Runtime": "python3.12",
                    "Role": {"Fn::GetAtt": ["Role", "Arn"]},
                },
            },
            "Role": {"Type": "AWS::IAM::Role", "Properties": {}},
            "Api": {"Type": "AWS::ApiGateway::RestApi", "Properties": {"Name": "x"}},
        },
        "Outputs": {"ApiUrl": {"Value": "https://example"}},
    })


# Oversized AND spec-conforming: the escalated build must clear the B23
# literal gate too — size was its only problem.
OVERSIZED_CONFORMING_SOURCE = CONFORMING_SOURCE + (
    "\n\ndef _padding():\n" + "    value = 1  # padding to overflow the inline ceiling\n" * 80
)
assert len(OVERSIZED_CONFORMING_SOURCE) > MAX_INLINE_CHARS


class EscalatingProvider(QualityProvider):
    """Inline passes emit OVERSIZED handlers; the packaged re-plan emits
    `packaged_source` (default: oversized-but-conforming code — fine when
    packaged) and a manifest derived the way the real provider does it."""

    def __init__(self, packaged_source: str = OVERSIZED_CONFORMING_SOURCE):
        super().__init__([OVERSIZED_CONFORMING_SOURCE, OVERSIZED_CONFORMING_SOURCE])
        self.packaged_source = packaged_source
        self.plan_profiles: list[str] = []
        self.plan_ctx_flags: list[tuple[bool, bool]] = []

    async def plan(self, ctx: BuildCtx, *, profile: str = "inline-cfn") -> BuildPlan:
        self.plan_profiles.append(profile)
        self.plan_ctx_flags.append((ctx.require_packaged, ctx.size_escalated))
        files = [PlannedFile(path="src/handler.py", brief="the handler")]
        if profile == "packaged-cfn":
            files.append(PlannedFile(path="requirements.txt", brief="deps", language="text"))
        files.append(PlannedFile(path="README.md", brief="docs", language="markdown"))
        return BuildPlan(app_name="quality-app", architecture_notes="n", files=files)

    async def generate_file(self, ctx, plan, file, generated):
        if file.path == "requirements.txt":
            from app.services.codegen.internal import (
                SizeEscalationDependencyError,
                third_party_imports,
            )

            modules = third_party_imports(generated)
            if modules and ctx.size_escalated:
                raise SizeEscalationDependencyError(modules)
            return "" if not modules else "requests==2.32.5\n"
        if ctx.size_escalated and file.path == "src/handler.py":
            self.file_calls += 1
            return self.packaged_source
        return await super().generate_file(ctx, plan, file, generated)

    async def assemble_template(self, ctx, plan, generated):
        if ctx.require_packaged:
            return _packaged_template_for("handler")
        return _template_embedding(generated["src/handler.py"])


async def _fake_packaging(build_id, files, requirements_txt):
    assert requirements_txt == "", "escalated builds must package with an EMPTY manifest"
    return {
        "zip_sha256": "f" * 64, "zip_bytes": 2048, "packages": [],
        "codebuild_id": "marshal-codegen-runner:fake", "artifact_s3_key": "artifacts/x/package.zip",
    }


async def test_size_escalation_rebuilds_as_packaged(
    db_session, test_user, client_for, audit_db, monkeypatch
):
    from app.services.codegen import packaging as packaging_svc

    provider = EscalatingProvider()
    monkeypatch.setattr(runner, "get_provider", lambda name=None: provider)
    monkeypatch.setattr(runner.get_settings(), "codegen_workspace_bucket", "probe-bucket", raising=False)
    monkeypatch.setattr(packaging_svc, "run_packaging", _fake_packaging)
    project = await _project_with_reqs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        assert r.status_code == 202, r.text
        assert r.json()["artifact_profile"] == "inline-cfn"  # started inline
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
        assert build.status == "ready", build.error
        # The row now tells the truth the deployer needs
        assert build.artifact_profile == "packaged-cfn"
        assert build.manifest["packaged"] is True
        esc = build.manifest["size_escalation"]
        assert esc["from"] == "inline-cfn" and esc["to"] == "packaged-cfn"
        assert esc["resolved"] is True  # live 13.5AA: first run left this False
        assert esc["oversized"] == {"src/handler.py": len(OVERSIZED_CONFORMING_SOURCE)}
        # compaction was tried first, honestly recorded as unresolved
        assert build.manifest["inline_retry"]["resolved"] is False
        # planner was told the profile AND the escalation posture
        assert provider.plan_profiles == ["inline-cfn", "packaged-cfn"]
        assert provider.plan_ctx_flags[1] == (True, True)
        # the inline attempt's artifacts were replaced, not accumulated
        r = await client.get(f"/api/v1/builds/{build.id}/artifacts")
        paths = sorted(a["path"] for a in r.json()["items"])
        assert paths == sorted([
            "README.md", "src/handler.py", "requirements.txt", "template.json",
            "package.zip", "package-manifest.json",
        ])
        r = await client.get(f"/api/v1/builds/{build.id}/artifacts/requirements.txt")
        assert r.json()["content"] == ""


async def test_size_escalation_refuses_third_party_imports(
    db_session, test_user, client_for, audit_db, monkeypatch
):
    """Escalation lifts the SIZE limit only: regenerated handlers that import
    third-party modules fail by name — the spec phrase is the only door."""
    from app.services.codegen import packaging as packaging_svc

    sneaky = "import requests\n" + OVERSIZED_CONFORMING_SOURCE
    provider = EscalatingProvider(packaged_source=sneaky)
    monkeypatch.setattr(runner, "get_provider", lambda name=None: provider)
    monkeypatch.setattr(runner.get_settings(), "codegen_workspace_bucket", "probe-bucket", raising=False)
    packaged_calls = []

    async def must_not_package(*a, **k):
        packaged_calls.append(1)

    monkeypatch.setattr(packaging_svc, "run_packaging", must_not_package)
    project = await _project_with_reqs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
    assert build.status == "failed"
    assert build.error["code"] == "validation_failed"
    msg = build.error["findings"][0]["message"]
    assert "requests" in msg and "SHALL use packaged dependencies" in msg
    assert build.manifest["size_escalation"]["resolved"] is False
    assert packaged_calls == []  # never spent CodeBuild on a refused build


async def test_size_escalation_unavailable_without_runner_infra(
    db_session, test_user, client_for, audit_db, monkeypatch
):
    """No workspace bucket → the pre-13.5AA behavior: fail with the finding."""
    provider = EscalatingProvider()
    monkeypatch.setattr(runner, "get_provider", lambda name=None: provider)
    monkeypatch.setattr(runner.get_settings(), "codegen_workspace_bucket", "", raising=False)
    project = await _project_with_reqs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
    assert build.status == "failed" and build.artifact_profile == "inline-cfn"
    assert provider.plan_profiles == ["inline-cfn"]
    assert "size_escalation" not in build.manifest
    assert any("inline ceiling" in f["message"] for f in build.error["findings"])


async def test_size_escalation_skipped_on_mixed_findings(
    db_session, test_user, client_for, audit_db, monkeypatch
):
    """An oversized handler PLUS an allowlist breach is not a size-only failure."""

    class MixedProvider(EscalatingProvider):
        async def assemble_template(self, ctx, plan, generated):
            doc = json.loads(_template_embedding(generated["src/handler.py"]))
            doc["Resources"]["Rogue"] = {"Type": "AWS::RDS::DBInstance", "Properties": {}}
            return json.dumps(doc)

    provider = MixedProvider()
    monkeypatch.setattr(runner, "get_provider", lambda name=None: provider)
    monkeypatch.setattr(runner.get_settings(), "codegen_workspace_bucket", "probe-bucket", raising=False)
    project = await _project_with_reqs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
    assert build.status == "failed" and build.artifact_profile == "inline-cfn"
    assert provider.plan_profiles == ["inline-cfn"]
    assert "size_escalation" not in build.manifest
