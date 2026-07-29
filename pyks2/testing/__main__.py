"""Allow ``python -m pyks2.testing`` as a shorthand for
``python -m pyks2.testing.simulator``."""

from .simulator import main

if __name__ == "__main__":
    raise SystemExit(main())
