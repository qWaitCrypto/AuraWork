# aura_pptx design guide (create mode)

Use this guide **before** writing a create-from-scratch plan. It defines safe defaults for slide size, typography, spacing, and palette choices. Keep decks clean and consistent.

## 1) Baseline setup
- Slide size: 13.333 x 7.5 inches (16:9)
- Grid: 12-column layout, 0.6 in outer margin
- Default font: Calibri (fallback: Arial)

## 2) Type scale
Use a consistent type scale across the deck.

| Role | Size (pt) | Weight | Notes |
| --- | --- | --- | --- |
| Title | 40-44 | Bold | One line if possible |
| Section Title | 32-36 | Bold | Avoid wrapping |
| Body | 18-22 | Regular | 1.2-1.3 line spacing |
| Caption | 14-16 | Regular | Subtle emphasis |

## 3) Spacing rules
- Title top margin: 0.5-0.8 in
- Body block top margin: 1.4-1.8 in
- Card padding: 0.3-0.5 in
- Minimum gap between elements: 0.2 in

## 4) Color palettes (safe defaults)
Pick one palette and stick with it.

### Palette A (Blue / Slate)
- Primary: 0F172A
- Accent: 2563EB
- Muted: 94A3B8
- Background: F8FAFC

### Palette B (Teal / Graphite)
- Primary: 111827
- Accent: 0EA5A4
- Muted: 6B7280
- Background: F3F4F6

### Palette C (Indigo / Warm)
- Primary: 1F2937
- Accent: 7C3AED
- Muted: 9CA3AF
- Background: F9FAFB

## 5) Layout recipes

### Title slide
- Large title at (0.7, 0.6) in
- Subtitle at (0.7, 1.6) in
- Optional footer tag at (0.7, 6.4) in

### Agenda slide
- Title top-left
- Two-column list grid with 4-6 bullets

### Two-column detail
- Left column: bullets or narrative
- Right column: image or chart

### Metrics slide
- Three cards in a row
- Each card: number + label + short note

## 6) Visual rules
- Keep shapes flat (no gradients unless required)
- Use 1-2 accent colors max per slide
- Use consistent stroke colors for boxes (e.g., D1D5DB)

## 7) When to use placeholders vs. textboxes
- If a template deck exists: use placeholders
- If creating from scratch: prefer textboxes with explicit coordinates

