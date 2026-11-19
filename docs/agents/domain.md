# Domain Docs

How engineering skills consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- `GLOSSARY.md` at the repo root.
- `docs/adr/` for ADRs that touch the work area.

If either is absent, continue with the authoritative owner decisions in `FORK_CONTRACT.md`.

## File structure

This is a single-context repository:

```text
/
|-- GLOSSARY.md
|-- docs/
|   `-- adr/
`-- backend/
```

Use the glossary's vocabulary in specs, tickets, code, tests, and reviews. If a newly introduced concept is absent from the glossary, resolve the wording before publishing dependent artifacts.

Surface any conflict with an existing ADR explicitly before changing its decision.
