import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
DAM_REFS = "/workspace/dam-refs"
DATASETS = os.path.join(DAM_REFS, "datasets")
