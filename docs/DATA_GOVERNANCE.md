# Data governance and release checklist

## Mandatory boundary

Treat MIMIC source notes, SCRs, private manifests, and dialogues derived from them as restricted
clinical research data. They must remain inside the approved research environment. The default
MedDial path uses `LocalOpenAICompatibleProvider`; external providers fail the
`restricted_clinical` boundary check.

## Repository remediation

The current branch removes previously tracked GTMF/SCR Markdown, generated dialogue Markdown,
and the bundled MTS-Dialog CSV from the working tree. CI prevents these artifact classes from
being added again. Deletion from a new commit does not remove content from earlier Git history.
Before making a research release, the repository owner should:

1. Temporarily restrict repository visibility.
2. Ask the institutional privacy/data-governance contact whether the historical GTMFs,
   dialogues, and thesis/poster examples are permitted public derivatives.
3. If they are not permitted, create a verified backup and perform an owner-approved history
   purge of the exact clinical-artifact paths.
4. Rotate any credentials found during a secret scan.
5. Verify from a fresh clone that no restricted artifact or source identifier remains.
6. Restore public visibility only after approval.

History rewriting is intentionally not automated by this PR because it affects every clone and
requires explicit repository-owner coordination.

## Manifests

- Private manifests contain MIMIC identifiers, remain ignored by Git, and support reproducible
  selection inside the credentialed environment.
- Release manifests contain salted study IDs and aggregate metadata only.
- Never commit the publication salt.

## Model calls

- MIMIC/SCR/dialogue: local controlled inference only.
- Public or wholly synthetic data: an external provider may be explicitly selected.
- Tests: mocked providers only.
