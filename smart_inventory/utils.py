"""Small shared helpers."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def to_py(obj: Any) -> Any:
    """Recursively convert numpy/pandas scalars to Python and NaN/NaT/inf to None.

    Needed before writing engine output to the database (psycopg2 rejects numpy
    types) and before returning JSON (NaN is not valid JSON).
    """
    if isinstance(obj, dict):
        return {k: to_py(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_py(v) for v in obj]
    if isinstance(obj, np.generic):  # before float/int checks: np.float64 subclasses float
        return to_py(obj.item())
    if obj is None or isinstance(obj, (str, bytes, bool, int)):
        return obj
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if obj is pd.NaT:
        return None
    return obj
