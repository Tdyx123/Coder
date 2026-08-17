# Task Data Engine Method Overview Design

## Objective

Reorganize `diagrams/01.drawio` from an implementation-heavy flowchart into a paper-ready Method Overview that communicates one primary path:

`Environment Resources → Task Data Engine → Structured Task Record → QA Generator (System Prompt + LLM) → QA Pairs`.

## Information Architecture

- Section labels: `(a) Task Data Generation` and `(b) QA Construction` only; no figure title, brackets, outer panels, divider, footer, or legend.
- Environment Resources: one blue container with four equal rows—Scenes, Objects, Skills, Robots—and consistent single-color outline icons.
- Task Data Engine: one thin teal container containing five numbered, icon-led stages in a single horizontal flow: Object–Skill Grounding, Task Synthesis, Constraint Filtering, Robot Assignment, State Construction. The five stage cards are removed.
- `1–5 subtasks` appears as a small dashed annotation below Task Synthesis. Robot Assignment uses repeated robot-arm/robot symbols, dashed assignment arrows, and subtask circles to communicate heterogeneous allocation.
- Structured Task Record: a teal folded document containing exactly Task instruction, Subtasks, Robot assignment, Initial / goal state, and Action sequence, with one compact field icon per row.
- QA Generator: a compact orange-tinted module inside section (b), explicitly showing `System Prompt → LLM`. The Structured Task Record enters this module before the output branches to the three QA cards.
- QA Construction: three equal orange cards—Decomposition QA, Allocation QA, PDDL QA—with one mapping line each.
- The engine as a whole outputs the record. The QA Generator connects to the QA cards through one trunk and a three-way branch.
- A single light-gray vertical dashed divider separates sections (a) and (b), following the new reference image.

## Icon Sources and Adaptation

- Primary source: [Tabler Icons](https://tabler.io/icons), MIT license. Selected source icons include `building`, `box`, `hand-grab`, `robot`, `link`, `list-details`, `message`, `flag`, `player-play`, `hierarchy-3`, `file-code`, `settings`, `brain`, and `terminal-2`.
- Secondary reference for robot/manufacturing metaphors: [Google Material Symbols](https://github.com/google/material-design-icons), Apache License 2.0.
- Source icons are embedded as SVG paths, recolored to the figure palette, normalized to a common 24 × 24 view box and stroke weight, and may be combined with simple arrows, circles, or labels. No remote image URL is retained in the Draw.io file.
- The Draw.io XML includes a source comment and `iconSource` metadata on library-derived icon cells.

## Visual System

- Canvas: 1920 × 1080, 16:9, pure white, no grid and no shadow.
- Typeface: Arial/Helvetica sans-serif with no more than three text levels.
- Colors: blue `#3B6FB6`, teal `#16837A`, orange `#D97818`, body text `#252525`, arrows `#3F3F3F`, secondary text/lines `#8A8A8A`, light fill `#F7F9FA`.
- Shapes: white or near-white fill, 1.2–1.5 pt colored borders, restrained corner radii, equal sizing and spacing. Stage icons replace stage cards.
- Connectors: dark-gray solid orthogonal arrows for the primary pipeline; short teal/orange dashed arrows are allowed only inside Robot Assignment and QA Generator illustrations.

## Acceptance Criteria

- The four-level narrative is legible within 5–10 seconds.
- The Engine is icon-led and contains no five stage cards.
- All required labels and only the five Structured Task Record fields are present.
- The visible QA flow contains `System Prompt`, `LLM`, and `QA Generator`, and every QA card is downstream of that generator.
- Library-derived icon cells retain `iconSource` metadata and the XML contains a Tabler MIT / Material Symbols Apache-2.0 attribution comment.
- The Draw.io XML parses successfully and remains editable.
- A raster preview confirms no overlaps, clipped labels, crossing connectors, grids, shadows, or unintended page title.
