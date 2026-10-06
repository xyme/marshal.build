"""Per-document validators (multi-doc spec R1.3–R1.5)."""

from app.services.spec_validation import strip_fences, validate_doc

VALID_REQUIREMENTS = """# Requirements — App
## Introduction
x
## Functional Requirements
### FR-1: A
**User story:** As a user, I want x, so that y.
**Acceptance criteria:**
1. WHEN a THEN the system SHALL b.
## Non-Functional Requirements
- fast
## Constraints
- none
## Assumptions
- single tenant
"""

VALID_DESIGN = """# Design — App
## Architecture Overview
Simple.
```mermaid
graph TD
  A[Web] --> B[API]
```
## Component Breakdown
### API
handles requests
## Data Model
- users(id)
## API Contracts
GET /things
## Technology Choices
| Concern | Choice | Rationale |
| api | Lambda | serverless |
## Security Considerations
- Cognito auth
"""

VALID_TASKS = """# Implementation Plan — App
- [ ] 1. Scaffold project _(Req: FR-1)_
  - setup repo
  - Test: builds green
- [ ] 2. Build API _(Req: FR-1)_
  - implement endpoints
  - Test: integration test passes
"""


def test_valid_docs_pass():
    assert validate_doc("requirements", VALID_REQUIREMENTS) == []
    assert validate_doc("design", VALID_DESIGN) == []
    assert validate_doc("tasks", VALID_TASKS) == []


def test_design_requires_mermaid():
    broken = VALID_DESIGN.replace("```mermaid", "```text")
    problems = validate_doc("design", broken)
    assert any("mermaid" in p for p in problems)


def test_design_requires_sections():
    problems = validate_doc("design", "# Design — App\njust vibes")
    assert any("Architecture Overview" in p for p in problems)
    assert any("Security Considerations" in p for p in problems)


def test_tasks_require_checkboxes():
    problems = validate_doc("tasks", "# Implementation Plan — App\n1. do stuff")
    assert any("checkbox" in p.lower() for p in problems)


def test_empty_fails():
    assert validate_doc("requirements", " ") == ["Document is empty"]


def test_strip_fences():
    assert strip_fences(f"```markdown\n{VALID_TASKS}\n```").startswith("# Implementation Plan")
    assert strip_fences(VALID_TASKS) == VALID_TASKS.strip()
