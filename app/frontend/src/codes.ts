// Plain-language messages for the coded refusals the app and the DA jobs return (SCHEMA_CONTRACT §7 items 19-21).
// Codes are mirrored verbatim from the DA (dssmp_check / cdse_load / the reversion publisher / 10b readiness view);
// unknown codes fall back to the raw code + message so nothing is hidden.
export const CODE_MESSAGES: Record<string, string> = {
  // DSSMP (ref.dssmp_version checks: go-live, flag apply, registration load)
  dssmp_missing: "The rule has no DSSMP version. Re-version it under the dam's current DSSMP before it can be published or edited.",
  dssmp_not_found: "That DSSMP version is not in ref.dssmp_version: it is unregistered, awaiting CDSE approval, or not yet loaded by the data layer.",
  dssmp_not_yet_effective: "That DSSMP version is approved but its effective date is in the future; it cannot govern rules yet.",
  dssmp_status_unknown: "That DSSMP version has a status the app does not recognise; it is treated as not approved (fail closed).",
  dssmp_draft: "That DSSMP version is still a draft and not approved.",
  dssmp_superseded: "That DSSMP version has been superseded; rules under it must be re-versioned.",
  dssmp_other_dam: "That DSSMP version belongs to a different dam.",
  dssmp_unverifiable: "ref.dssmp_version could not be read, so the check failed closed. Try again later.",
  dssmp_document_hash_mismatch: "The DSSMP document does not match its sha256. Register the version again with the correct document.",
  // go-live readiness (ref.v_dam_go_live_readiness)
  no_cdse_in_reviewer_authority: "The dam has no CDSE in the reviewer authority. Assign the dam's CDSE (and let the data layer load it) before go-live.",
  go_live_readiness_missing: "The dam is not in the go-live readiness view yet (it is not in ref.dam and has no CDSE assignment). Assign its first CDSE.",
  go_live_readiness_unverifiable: "The go-live readiness view could not be read, so go-live is blocked (fail closed). Try again later.",
  go_live_not_ready: "The go-live readiness view reports the dam as not ready.",
  // CDSE load job refusals (dam_cdse_outbox.reason_code)
  cdse_exists: "The data layer refused the assignment: the dam already has a CDSE. Use a CDSE change instead.",
  cdse_stale: "The dam's CDSE changed since this was proposed. Propose the change again against the current CDSE.",
  approval_record_missing: "The data layer could not find the signed approval record for this row in the review chain.",
  approval_record_wrong_action: "The linked approval record is for a different action than this row.",
  approval_record_mismatch: "The row does not match what the signed approval record says (dam, CDSE values or reason).",
  approval_not_authorized: "The approver was not authorized for this dam when the record was signed.",
  invalid_value: "A value in the row is not valid (e.g. a malformed principal, hash or reason code).",
  missing_required_column: "A required column of the row is empty.",
  cdse_unverifiable: "The dam's designated CDSE could not be read, so the action was refused (fail closed).",
  cdse_authority_missing: "The dam has no designated CDSE yet; assign the first CDSE instead.",
  // rule re-version publisher refusals (ref_change_publication.message '<code>: <detail>')
  reversion_approval_missing: "The publisher could not find the CDSE approval record for this re-version.",
  reversion_approval_wrong_action: "The linked record is not a re-version approval.",
  reversion_approval_mismatch: "The publication does not match the approved re-version (rules, versions or target DSSMP).",
  reversion_not_authorized: "The approver was not the dam's CDSE when the re-version was approved.",
  reversion_fields_changed: "The new rule version changes more than its DSSMP version; a re-version must copy every other field unchanged.",
  payload_invalid: "The publication payload is malformed and was refused.",
  unknown_kind: "The publisher does not know this publication kind.",
  missing_not_null_column: "The new rule version lacks a column ref.threshold requires (NOT NULL).",
  unknown_columns: "The new rule version carries columns ref.threshold does not have.",
  reversion_stale: "A newer version of an affected rule was published since the proposal; propose the re-version again.",
};

export function explain(code?: string | null): string | null {
  return code ? CODE_MESSAGES[code] ?? null : null;
}

/** '<code>: <detail>' -> code (DA publisher / load-job message convention), else null */
export function codeOf(message?: string | null): string | null {
  const m = /^([a-z][a-z0-9_]*):/.exec(message || "");
  return m ? m[1] : null;
}
