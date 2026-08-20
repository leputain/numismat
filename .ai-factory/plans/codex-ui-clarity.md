# Implementation Plan: Premium UI Clarity

Branch: codex/ui-clarity
Created: 2026-08-20

## Original Request

Еще улучши интерфейс, сделай красивее и понятнее!

## Settings

- Testing: yes, focused frontend checks plus one final production build
- Logging: minimal; production client telemetry remains no-op and no financial or identity data is added
- Docs: no, unless a user-facing navigation contract materially changes

## Scope and Decisions

- Keep the existing React/Vite stack, generated OpenAPI types, API contracts, security boundaries and multi-tenant session binding unchanged.
- Use a calm private-finance-journal visual direction: Telegram theme colors, ink-like surfaces, one warm coin accent and semantic income/expense colors.
- Never mix currencies, invent financial values, or turn analytics into decorative fake data.
- Prefer small repository-native SVG icons and existing CSS over a new icon, chart or component dependency.
- Remove implementation jargon from the user layer while preserving the underlying bounded, review-first and idempotent behavior.
- Preserve the unrelated local changes in OCR and Telegram adapter files and exclude them from UI commits.

## Tasks

### Phase 1: Visual system and navigation

- [x] Task 1: Refine theme tokens, shell, reusable icons, responsive navigation, focus/touch states and Telegram-native BackButton routing for every deep screen. Keep changes backward-compatible with existing routes and component APIs. Logging: no new runtime events or values.

### Phase 2: Financial overview and analytics

- [x] Task 2: Recompose the dashboard first viewport around the real monthly result, active draft and useful secondary actions; improve analytic hierarchy, trends and category readability without aggregating currencies. Logging: retain the existing fixed page-open event codes only.

### Phase 3: Operations and guided entry

- [x] Task 3: Improve transaction cards/feed clarity, add a derived draft progress guide, replace engineering copy with plain Russian and polish empty/error/form states without changing mutation payloads or workflow states. Logging: no input, finance, cursor, revision or identity data is emitted.

### Phase 4: Focused verification

- [x] Task 4: Run focused Vitest/typecheck, the canonical frontend check and production build; visually inspect mobile dark/light layouts and deep-route navigation, then verify no source maps, API drift or unrelated-file staging. Logging: inspect only fixed local build/test output and never runtime secrets.
