You are producing a spec.md from company-format capability source documents.

Capability: {title} ({slug})

Source material:

## Business Requirements
{business_requirement}

## High-Level Design (Engineering Requirements)
{hld}

## Business Spec / Acceptance Criteria
{business_spec}

## Notes
{notes}

---

Combine the four source documents into one coherent spec.md. Include ALL of:

1. All BRs — verbatim, with BR-NNN IDs preserved
2. All ERs with their Satisfies/Consumes links — verbatim, with ER-NNN IDs preserved
3. All ACs — verbatim, with AC-NNN IDs and BR trace links preserved
4. Dependencies table from the High-Level Design section
5. Open questions from the High-Level Design section (clearly marked as a section)

Preserve all IDs exactly as written. Do not summarise or paraphrase — your job is to
restructure, not rewrite. If a section is absent from the source material, omit it from
the output rather than inventing content.

Output ONLY the markdown content of spec.md. No preamble, no code fences, no commentary.
