.PHONY: backend frontend test

backend:
	cd backend && uv run uvicorn app.main:app --reload

frontend:
	cd frontend && npm run dev

test:
	cd backend && uv run pytest
	cd frontend && npm run test && npm run build
	cd tools/dukascopy_downloader && uv run pytest
