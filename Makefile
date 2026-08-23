.PHONY: backend frontend test openapi e2e

backend:
	cd backend && uv run uvicorn app.main:app --reload

frontend:
	cd frontend && npm run dev

test:
	cd backend && uv run pytest
	cd frontend && npm run test && npm run build
	cd tools/dukascopy_downloader && uv run pytest

openapi:
	cd backend && uv run python -m scripts.export_openapi
	cd frontend && npm run generate:types

e2e:
	cd frontend && npm run test:e2e
