# Canonical vocabulary

[`UBIQUITOUS_LANGUAGE.md`](../../UBIQUITOUS_LANGUAGE.md) is canonical for every domain term,
**including code identifiers**. A word from its "Aliases to avoid" column appearing as a class,
function, field, variable, or state name — or in prose, prompts, or docs — is a defect, not a style
nit. (No `paths` scope on purpose: this binds code *and* writing.)

## Define once, point thereafter

A term is **defined** in exactly one place — [`UBIQUITOUS_LANGUAGE.md`](../../UBIQUITOUS_LANGUAGE.md)
— and its rationale **argued** in exactly one — [`docs/design.md`](../../docs/design.md). Every other
mention (code comments, docstrings, `README`, [`docs/architecture.md`](../../docs/architecture.md),
tests) *uses* the term and may state a **local** fact about the code in front of it, but must not
re-define the term or re-argue the decision behind it. A comment or docstring that re-explains what a canonical term *means* is a DRY violation
in prose, exactly as duplicated logic is in code.
