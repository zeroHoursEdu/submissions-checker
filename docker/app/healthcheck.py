"""Container health probe for the production image.

Deliberately uses only the standard library: nothing is installed in the runtime image
solely to support the health check, and the probe cannot break because a tool was
trimmed from the image.

Exits 0 when the application answers /health with a 2xx, non-zero otherwise — including
while the application is still starting, which is what the Dockerfile's start period and
the proxy's passive health checks are for.
"""

import os
import sys
import urllib.error
import urllib.request

URL = f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/health"

try:
    with urllib.request.urlopen(URL, timeout=4) as response:  # noqa: S310 - fixed localhost URL
        sys.exit(0 if 200 <= response.status < 300 else 1)
except (urllib.error.URLError, OSError, ValueError) as exc:
    print(f"healthcheck failed: {exc}", file=sys.stderr)
    sys.exit(1)
