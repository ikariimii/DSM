-- Canonical instrument_type vocabulary (SCHEMA_CONTRACT v1.1 §5.4; app open question 8).
-- Codes are lower snake_case (the app's onboarding vocabulary); units and sign conventions follow
-- /workspace/dam-refs/CONVENTIONS.md ("Units and symbols", "Sign conventions"). dam_refs_family =
-- /workspace/dam-refs/datasets/<dir>. engine_formulas lists ref.formula ids that reduce this type;
-- an empty array means the data layer has NO verified engine formula yet (app must not compute).
USE CATALOG IDENTIFIER(:catalog);
CREATE TABLE IF NOT EXISTS ref.instrument_type (
  instrument_type   STRING NOT NULL,
  description       STRING NOT NULL,
  eng_units         ARRAY<STRING> NOT NULL,
  sign_convention   STRING NOT NULL,
  dam_refs_family   STRING,
  engine_formulas   ARRAY<STRING> NOT NULL,
  legacy_codes      ARRAY<STRING> COMMENT 'upper-case codes used in earlier DDL comments / FERC canvas',
  CONSTRAINT pk_instrument_type PRIMARY KEY (instrument_type)
);
MERGE INTO ref.instrument_type t USING (VALUES
  ('piezometer', 'Vibrating-wire / pneumatic / standpipe piezometer (subtype vw|standpipe|pneumatic)', array('m','kPa'), 'pressure compression positive; elevation m above project datum', 'piezometers', array('VW_PIEZO','STANDPIPE_DTW'), array('VW_PIEZOMETER','STANDPIPE','PIEZOMETER')),
  ('reservoir_level', 'Headwater / tailwater level sensor', array('m'), 'elevation m above project datum (datum name recorded)', '_shared', array('LEVEL_DATUM'), array('WATER_LEVEL','LEVEL')),
  ('seepage_weir', 'Seepage weir (90 deg V-notch or Cipolletti)', array('L/s','ft3/s'), 'flow positive', 'seepage', array('WEIR_VNOTCH'), array('SEEPAGE_WEIR','WEIR','SEEPAGE')),
  ('inclinometer', 'Probe or in-place inclinometer', array('mm'), 'A0 direction positive (downstream); cumulative from fixed bottom', 'inclinometers', array('INCL_PROBE_2PASS'), array('INCLINOMETER')),
  ('crackmeter', 'Crackmeter / jointmeter / VW displacement transducer', array('mm'), 'opening positive, closing negative', 'extensometers', array('VW_DISP_LIN'), array('CRACKMETER','JOINTMETER')),
  ('extensometer', 'Borehole / rod extensometer', array('mm'), 'extension positive; reference anchor (deepest or collar) stated', 'extensometers', array('VW_DISP_LIN'), array('EXTENSOMETER')),
  ('settlement', 'Settlement point / survey monument / GNSS / settlement cell', array('mm'), 'settlement (down) positive in eng_value; raw up-positive kept in raw_value', 'settlement-survey-gnss', array('SETTLEMENT_DELTA'), array('SETTLEMENT','SURVEY_MONUMENT','GNSS')),
  ('strain_gauge', 'Embedment / spot-weldable VW strain gauge (e.g. Geokon 4000/4200)', array('microstrain'), 'tension positive (structural); flag manufacturer differences', 'strain-stress-load', array(), array('SG','STRAIN_GAUGE','STRAINMETER')),
  ('pressure_cell', 'Total pressure / stress cell', array('kPa'), 'compression positive', 'strain-stress-load', array(), array('PRESSURE_CELL','STRESS_CELL')),
  ('load_cell', 'Anchor / tendon load cell', array('kN'), 'compression positive', 'strain-stress-load', array(), array('LOAD_CELL')),
  ('tiltmeter', 'Tiltmeter / tilt beam', array('arcsec','mm/m'), 'rotation toward A+ (downstream) positive; unit stated', 'tiltmeter', array(), array('TILTMETER')),
  ('plumbline', 'Plumb / inverted plumb line with coordinate reader', array('mm'), 'downstream / right-abutment positive per structure axes (stated)', 'plumbline', array(), array('PLUMBLINE','PENDULUM')),
  ('accelerometer', 'Strong-motion accelerometer (event records, not alarm-evaluated)', array('g','cm/s2'), 'per instrument axis convention (stated)', 'accelerometer_strong_motion', array(), array('ACCELEROMETER','STRONG_MOTION')),
  ('thermometer', 'Temperature sensor / thermistor channel', array('degC'), 'n/a', NULL, array(), array('THERMISTOR','TEMPERATURE'))
) AS s(instrument_type, description, eng_units, sign_convention, dam_refs_family, engine_formulas, legacy_codes)
ON t.instrument_type = s.instrument_type
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;
