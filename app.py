"""Backward-compatible local entry point.

Vercel uses api/index.py. This module remains for local development and
for tooling that historically imported ``app`` from the repository root.
"""

from website import app

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
