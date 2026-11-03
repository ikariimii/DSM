-- SQL alert dam_data_stale: ADAS sensors with no reading for > 2x expected interval (REQ-18..21).
SELECT count(*) AS n_stale
FROM ${catalog}.gold.data_freshness
WHERE is_stale
