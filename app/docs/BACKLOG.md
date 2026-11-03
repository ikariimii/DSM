# App backlog

## B-1 Document signing for exports (PDF / certificate-based)
**Status:** backlog, not started. **Owner:** to assign. **Source:** lead decision on QA-039 (no in-app cryptographic
signature; FERC confirmed none is needed in-app).

- Scope: certificate-based (PKI) signing of the **final DSSMR export** (FERC Engineering Guidelines Ch.14 Appendix K
  signature page) and **any PE-sealed document** the app exports.
- Done as a **document-signing export step** (e.g. PAdES-signed PDF with the signer's certificate and seal image), not
  as an in-app action: the app's reviewer actions stay hash-chained audit records (SCHEMA_CONTRACT §5a).
- Configurable per **state board rules** (seal / signature requirements differ by state), for example NCEES Model
  Rules 240.20 and Oregon OAR 820-005-0020. Each dam's state drives the rule set; the rule references must be checked
  against the current board text before implementation.
- Hooks already in place: nullable `signature`, `sig_alg`, `key_id` on `review_signoff` and the
  `backend.audit_chain.Signer` interface (no-op default) if a key-based record signature is ever wanted.
- Acceptance (draft): the signed PDF validates in a standard reader; the signature page lists the PE, licence number,
  state and date; the export records its own hash in the dam's audit chain.
