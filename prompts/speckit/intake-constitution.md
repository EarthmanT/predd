You are extracting a constitution.md from a company-format capability specification.

Capability: {title} ({slug})

Source material:

## Business Requirements
{business_requirement}

## High-Level Design (Engineering Requirements)
{hld}

## Business Spec / Acceptance Criteria
{business_spec}

## Notes and Term Definitions
{notes}

---

Produce the constitution.md. Include ONLY:

1. **Term definitions** from Notes — copy verbatim. Do not improvise synonyms or paraphrase.
2. **Architectural invariants** — rules the implementation must NEVER violate, not features to build.
   Examples: tenant isolation (no cross-tenant reads at any layer), deterministic scoring (no LLM
   opinions in scores), LLM participation boundary (discovery only, not scoring or validation).
3. **Hard thresholds** — specific numeric or categorical values baked into requirements that must
   not drift (e.g. <50% confidence flag, <5 blueprint low-confidence notice, ≥3-consecutive
   feedback window).

Do NOT include:
- Business requirements as BRs
- Engineering requirements (ERs)
- Acceptance criteria (ACs)
- Open questions
- Delivery timelines or sprint goals
- Feature descriptions

Output ONLY the markdown content of constitution.md. No preamble, no code fences, no commentary.
