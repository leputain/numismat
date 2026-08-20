# Implementation Plan: Interactive Financial Analytics

Branch: codex/analytics-charts
Created: 2026-08-20

## Original Request

Улучши аналитику, графики выглядят не очень, хочу более красивые и понятные. Чтобы можно было выбрать за неделю, месяц, год. Используй какие нить сторонние библиотеки. Чтобы графики были более достойные.
И скажи что еще улучшить?)

## Settings

- Testing: yes, focused deterministic frontend tests plus one final canonical frontend check and production build
- Logging: minimal; no financial values, period boundaries, currencies, identities or chart interactions enter production telemetry
- Docs: factual dependency and UX-contract updates only

## Scope and Decisions

- Reuse the existing owner-timezone-aware `/api/v1/reports/timeseries` contract; no backend or OpenAPI change is required.
- Add exact-pinned Recharts 3 with matching `react-is`, load the chart chunk lazily and keep the initial Mini App bundle lean.
- Offer explicit calendar periods to date: current week, month and year; use daily buckets for week/month and monthly buckets for year.
- Keep currencies strictly isolated. Never combine currency totals or use `float`/unsafe JavaScript numbers for money.
- Convert only bounded, dimensionless visual coordinates to `number`; retain exact BigInt values for labels, insights, tooltips and the accessible table.
- Preserve current authentication, session binding, generated API types, routes, query keys and mutation behavior.
- Preserve unrelated local OCR and Telegram adapter changes and exclude them from this feature commit.

## Tasks

### Phase 1: Contract and dependency

- [x] Task 1: Add exact-pinned Recharts dependencies, define the week/month/year period model from the server-provided time anchor and cover calendar/leap-year boundaries with focused tests. Logging: none.

### Phase 2: Chart model and rendering

- [x] Task 2: Build a BigInt-safe presentation model and lazy Recharts composed chart with income/expense bars, net trend, cumulative result, zero reference, exact tooltip, currency selector and accessible data fallback. Logging: no chart data or interactions.

### Phase 3: Analytics experience

- [x] Task 3: Recompose the analytics screen around a clear period selector, selected-period summary and compact insights; add polished responsive styles, loading/error/empty states and reduced-motion support without mixing currencies. Logging: retain only the existing fixed analytics-open event.

### Phase 4: Verification and delivery

- [x] Task 4: Run focused Vitest, API drift/type checks, dependency audit and production build; verify lazy chunks/no source maps, inspect mobile/tablet layouts in a browser and audit dark/light Telegram theme variables before isolated web deployment. Logging: inspect only fixed build/runtime status without financial or identity data.

### Phase 5: Modern fintech design iteration

- [x] Task 5: Replace the card-heavy analytics presentation with one result hero, a flat KPI strip and a cohesive chart suite; adopt a calmer mint/blue semantic palette and sans-first hierarchy, then repeat mobile/light/dark visual QA before commit and isolated web deployment. Logging: none.
