"""ETL Core package.

Loads .env into os.environ at import time so that modules reading raw
environment variables (clee_resolution_config.MATCH_WORKERS,
KB_MAX_ROWS_PER_PARTITION) see the
same values pydantic-settings reads. Shell-exported variables win:
load_dotenv() does not override existing environment entries.
"""

from dotenv import load_dotenv

load_dotenv()
