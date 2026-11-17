"""Run with python3 -m ritcx from the repository root."""

import sys

from .runtime import TradingError
from .session import main

try:
    raise SystemExit(main())
except TradingError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(1)
