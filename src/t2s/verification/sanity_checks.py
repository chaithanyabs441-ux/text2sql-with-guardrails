import pandas as pd
import logging
from typing import List

logger = logging.getLogger(__name__)

class ResultSanityChecker:
    def __init__(self):
        pass

    def check(self, df: pd.DataFrame, error_msg: str | None = None) -> List[str]:
        """
        Runs a series of sanity checks on the DataFrame.
        Returns a list of warning strings.
        """
        warnings = []

        # 1. Execution Error
        if error_msg:
            warnings.append(f"Database execution error: {error_msg}")
            return warnings

        # 2. Empty Result Set
        if df.empty:
            warnings.append("Query returned zero rows. Verify filters or joins.")
            return warnings

        # 3. Negative values in columns that should be positive (e.g., revenue, quantity)
        # We check any numeric column to be safe, flag if >50% are negative
        for col in df.columns:
            if pd.api.types.is_numeric_dtype(df[col]):
                if (df[col] < 0).sum() > (len(df) * 0.5):
                    warnings.append(f"Column '{col}' contains mostly negative values.")

        # 4. Excessively large result set (should be prevented by LIMIT 1000, but just in case)
        if len(df) > 1000:
            warnings.append("Result set exceeds 1000 rows. Check if a LIMIT should be applied.")

        # 5. High NULL ratio
        for col in df.columns:
            null_ratio = df[col].isnull().mean()
            if null_ratio > 0.5:
                warnings.append(f"Column '{col}' is >50% NULL. Possible bad JOIN.")

        logger.info(f"Sanity checks completed. Found {len(warnings)} warnings.")
        return warnings