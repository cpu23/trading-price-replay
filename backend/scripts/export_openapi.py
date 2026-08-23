"""Export the FastAPI app's OpenAPI schema to backend/openapi.json.

The exported schema is the single source of truth for the API contract: the
frontend generates its wire types from this file (openapi-typescript), and CI
re-runs this script to fail when the committed schema drifts from the code.
"""
import json
from pathlib import Path

from app.main import app


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "openapi.json"
    schema = app.openapi()
    out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(schema['paths'])} paths, {len(schema['components']['schemas'])} schemas)")


if __name__ == "__main__":
    main()
