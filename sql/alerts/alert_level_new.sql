-- SQL alert dam_alert_level_new: ALERT-level (level = 1) exceedances raised in the last 30 min.
SELECT count(*) AS n_new_alert
FROM ${catalog}.gold.threshold_exceedances
WHERE level = 1 AND unacknowledged AND updated_ts >= current_timestamp() - INTERVAL 30 MINUTES
