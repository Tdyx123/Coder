# Task Data Engine Method Overview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild `diagrams/01.drawio` as a clean, paper-ready Method Overview matching the approved reference hierarchy.

**Architecture:** Replace the existing page contents with a deliberately small mxGraph model rather than incrementally moving legacy nodes. Keep all icons as embedded/editable SVG image cells, use a single horizontal engine pipeline, and route the structured record through one branch trunk to three uniform QA cards.

**Tech Stack:** diagrams.net uncompressed mxGraph XML, embedded SVG icons, Draw.io CLI or compatible renderer for preview, XML parser checks.

## Global Constraints

- Preserve a 1920 × 1080, 16:9, pure-white canvas.
- Use only blue `#3B6FB6`, teal `#16837A`, orange `#D97818`, dark gray `#3F3F3F`, body text `#252525`, secondary gray `#8A8A8A`, and very light fill `#F7F9FA`.
- Use Arial/Helvetica sans-serif and at most three text levels.
- Keep the five engine stages independent, equal-sized, and connected by one left-to-right path.
- Use one record-to-QA trunk with three short orthogonal branches.
- Do not add a page title, bracket, outer panel, divider, legend, retry loop, feedback path, QA Builder, or implementation-detail field.

---

### Task 1: Rebuild and Validate the Method Overview

**Files:**
- Modify: `diagrams/01.drawio`
- Reference: `docs/superpowers/specs/2026-08-08-task-data-engine-method-overview-design.md`
- Create during verification only: `/tmp/01-method-overview.png`

**Interfaces:**
- Consumes: the approved labels, hierarchy, palette, and layout constraints in the design specification.
- Produces: one uncompressed, editable mxGraph document with diagram id `task-data-engine-qa` and page name `Task Data Engine and QA Construction`.

- [x] **Step 1: Establish a failing structural check against the legacy diagram**

Run:

```bash
rg -n 'Task Data Engine and QA Pair Construction|QA Builder|Reject &amp;amp; Resample|legend_generation' diagrams/01.drawio
```

Expected: matches are returned, demonstrating that legacy-only elements still exist.

- [x] **Step 2: Replace the legacy mxGraph page contents**

Use one root layer containing section labels; an Environment Resources container with four SVG icon rows; a Task Data Engine container with five equal stage cells and two small annotations; a document-shaped Structured Task Record; three uniform QA cards with distinct outline icons; dark-gray pipeline edges; and a single record-to-QA branch trunk.

- [x] **Step 3: Validate XML structure and required/forbidden content**

Run:

```bash
/home/dwb/.pyenv/bin/pyenv exec python -c "import xml.etree.ElementTree as ET; ET.parse('diagrams/01.drawio')"
rg -n 'Environment Resources|Object–Skill.*Grounding|Task Synthesis|Constraint.*Filtering|Robot Assignment|State Construction|Structured Task Record|Decomposition QA|Allocation QA|PDDL QA' diagrams/01.drawio
! rg -n 'Task Data Engine and QA Pair Construction|QA Builder|Reject &amp;amp; Resample|legend_generation|panel_a|panel_b|divider' diagrams/01.drawio
```

Expected: XML parsing succeeds, every required label matches, and no forbidden legacy element matches.

- [x] **Step 4: Export a visual preview**

Because no Draw.io CLI is installed, use the locally available Pillow runtime to produce `/tmp/01-method-overview.png` from the same canvas geometry at 1920 × 1080. Expected: command exits 0 and the PNG is non-empty. This is a diagnostic preview; `diagrams/01.drawio` remains the sole product file.

- [x] **Step 5: Inspect the preview and correct visual defects**

Confirm the preview has a white background; balanced section spacing; equal engine stages and QA cards; legible three-level typography; aligned icons; no clipping or overlap; and one unobstructed QA branch trunk. Repeat export after any correction.

- [x] **Step 6: Review the final diff**

Run:

```bash
git diff --check
git diff --stat -- diagrams/01.drawio
```

Expected: no whitespace errors and `diagrams/01.drawio` is the only product file changed.
