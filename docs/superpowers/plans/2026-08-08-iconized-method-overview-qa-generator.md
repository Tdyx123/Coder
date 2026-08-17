# Iconized Method Overview and QA Generator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update `diagrams/01.drawio` to match the new icon-led reference and explicitly show System Prompt plus LLM in QA generation.

**Architecture:** Keep the existing four-level method narrative, but replace the five engine cards with numbered icon stages and add field icons to the structured record. Insert one QA Generator module between the record and the three-way QA branch, containing the visible flow `System Prompt → LLM`. Embed adapted SVG paths from Tabler Icons as self-contained data URIs and retain source metadata.

**Tech Stack:** diagrams.net uncompressed mxGraph XML, embedded SVG data URIs, Tabler Icons (MIT), Material Symbols reference (Apache-2.0), Python XML validation, Pillow diagnostic preview.

## Global Constraints

- Preserve a 1920 × 1080, pure-white, editable Draw.io canvas.
- Use blue `#3B6FB6`, teal `#16837A`, orange `#D97818`, dark gray `#3F3F3F`, body text `#252525`, secondary gray `#8A8A8A`, and light fills only.
- Display exactly five numbered engine stages and exactly five Structured Task Record fields.
- Display `QA Generator`, `System Prompt`, and `LLM` as visible labels.
- Route the record through the QA Generator before the three-way QA branch.
- Store no external image URLs; all selected icons must be embedded SVG.
- Preserve Tabler/Material source attribution in XML comments and `iconSource` metadata.

---

### Task 1: Rebuild the Diagram with Library-Derived Icons and QA Generator

**Files:**
- Modify: `diagrams/01.drawio`
- Modify: `docs/superpowers/specs/2026-08-08-task-data-engine-method-overview-design.md`
- Reference: `/home/dwb/.codex/attachments/8fd82883-816a-48d4-8137-a9e56442d55d/codex-clipboard-f8d573a9-64b8-4b5a-ab93-b621cb2e9b4b.png`
- Create during verification only: `/tmp/01-iconized-method-overview.png`

**Interfaces:**
- Consumes: the current structured record and QA mappings, plus selected official Tabler SVG paths.
- Produces: one self-contained mxGraph document whose QA path is `record → qa_generator → qa_branch → three QA cards`.

- [x] **Step 1: Establish failing checks for the new requirements**

Run:

```bash
! rg -n 'QA Generator|System Prompt|LLM|iconSource="Tabler Icons' diagrams/01.drawio
```

Expected: the negated search succeeds because the current diagram has none of the new labels or source metadata.

- [x] **Step 2: Replace stage cards with icon-led stages**

Remove the five rounded stage cells. Add numbered labels, embedded Tabler-derived `link` and `list-details` icons, a funnel, a robot-assignment composition, and braces/state glyphs. Keep one dark-gray primary arrow between each stage.

- [x] **Step 3: Add record field icons and the QA Generator**

Render the five record fields as aligned icon/text rows. Add a light orange QA Generator containing a settings/terminal System Prompt symbol, a brain symbol labeled LLM, and a directional arrow. Connect the record only to the generator, then connect the generator to one three-way QA branch.

- [x] **Step 4: Update QA cards and source metadata**

Use a Tabler `hierarchy-3` icon for Decomposition QA, a combined robot-to-circle allocation illustration for Allocation QA, and a Tabler `file-code` icon for PDDL QA. Add an XML attribution comment and `iconSource` attributes to every library-derived icon cell.

- [x] **Step 5: Validate XML and structural requirements**

Run the repository pyenv Python to parse the XML and assert unique cell IDs, five numbered stage labels, five record rows, visible generator labels, `record → qa_generator` connectivity, `qa_generator → qa_branch` connectivity, embedded `data:image/svg+xml` images, and absence of HTTP image styles.

- [x] **Step 6: Generate and inspect a diagnostic preview**

Render `/tmp/01-iconized-method-overview.png` at 1920 × 1080 using the same geometry and inspect it for clipped labels, inconsistent icon sizes, overlapping arrows, unclear section boundaries, and unbalanced whitespace.

- [x] **Step 7: Review the final diff**

Run:

```bash
git diff --check
git diff --stat -- diagrams/01.drawio
```

Expected: no whitespace errors and the Draw.io file remains the only product file changed.
