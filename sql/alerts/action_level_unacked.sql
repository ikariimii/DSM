-- SQL alert (resources/alerts.yml: dam_action_level_unacked). Source: gold.threshold_exceedances.
-- Fires when ANY ACTION-level (level >= 2) alarm is unacknowledged. Evaluation: COUNT > 0.
-- Catalog is set by the alert's parent warehouse default / fully-qualified via bundle var substitution.
SELECT count(*) AS n_unacked_action
FROM ${catalog}.gold.threshold_exceedances
WHERE level >= 2 AND unacknowledged
