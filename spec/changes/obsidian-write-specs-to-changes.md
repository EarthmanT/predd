# Obsidian: Write generated specs to spec/changes/

## Problem

`obsidian analyze` generates an analysis report and writes it to `~/.config/predd/obsidian/`, then tells the user to manually create spec files in `spec/changes/`. The `spec_dir` variable is defined but never used. This breaks the self-improvement loop because generated specs never reach the directory where they'd be picked up.

## Solution

Update `obsidian_analyze()` to:

1. Ask Claude to output structured JSON with complete spec content (not just titles)
2. Parse the JSON response
3. Write each spec to `spec/changes/<filename>.md`
4. Keep the analysis summary in `~/.config/predd/obsidian/` for reference
5. Log a `spec_generated` decision event for each spec written
6. Skip specs whose filename already exists in `spec/changes/`

## Prompt Changes

Replace the current free-text analysis prompt with one requesting JSON output:

```json
{
  "analysis": "Brief summary of findings",
  "specs": [
    {
      "filename": "kebab-case-name.md",
      "title": "Human readable title",
      "content": "Full spec markdown with ## Problem, ## Solution, ## Implementation, ## Testing"
    }
  ]
}
```

## Output Handling

- Strip markdown fencing if present (models sometimes wrap JSON in ```json blocks)
- On JSON parse failure, fall back to writing raw text to obsidian dir
- Skip writing if `spec/changes/<filename>` already exists (no overwrite)
- Log `spec_generated` decision event per spec

## Testing

- Verify specs land in `spec/changes/`, not `~/.config/predd/obsidian/`
- Verify duplicate filenames are skipped
- Verify JSON parse failure falls back gracefully
- Verify `spec_generated` decision events are logged
