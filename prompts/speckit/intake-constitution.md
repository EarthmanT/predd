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
2. **Architectural invariants** — rules the implementation must NEVER violate, not features to
   build. Look for statements like "must never", "under no circumstances", "always", "required at
   every layer", or similar absolute language in the source material.
3. **Hard thresholds** — specific numeric or categorical values baked into requirements that must
   not drift (e.g. percentage cutoffs, count limits, time windows, retry counts).

Do NOT include:
- Business requirements as BRs
- Engineering requirements (ERs)
- Acceptance criteria (ACs)
- Open questions
- Delivery timelines or sprint goals
- Feature descriptions

Output ONLY the markdown content of constitution.md. No preamble, no code fences, no commentary.
