"""dam_safety: Databricks-native dam safety monitoring core library.

Pure-Python cores (no Spark import at module load) so everything is unit-testable
locally; Spark/pandas wrappers live in ``dam_safety.spark``.
"""
__version__ = "0.1.0"
