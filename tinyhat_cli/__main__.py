"""``python3 -m tinyhat_cli`` — what the /usr/local/bin/tinyhat wrapper execs."""

import sys

from tinyhat_cli.entrypoint import main

if __name__ == "__main__":
    sys.exit(main())
