# FastAPI Backend API (dt3_bootcamp_backend)

This container provides backend APIs for the **Commercial Energy Consumption Analytics & Anomaly Alerts** React dashboards.

> Current status: endpoints return **placeholder/demo data** so the React UI can be wired end-to-end. Persistence and real analytics will be added next.

## Run locally

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure CORS origins (optional)
cp .env.example .env
# edit .env as needed

uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000
```

- Swagger UI: `http://localhost:8000/docs`
- OpenAPI JSON: `http://localhost:8000/openapi.json`

## Environment variables

- `CORS_ORIGINS` (optional): comma-separated list of allowed web origins. Default: `http://localhost:5173`
- `API_PREFIX` (optional): API base path prefix. Default: `/api/v1`

## Implemented endpoints (v1)

### Health
- `GET /health`

### Ingestion
- `POST /api/v1/ingest/csv` (multipart upload)
  - Query: `site_id`
  - File field: `file` (CSV with `timestamp` and `kWh` columns)

### Consumer
- `GET /api/v1/consumer/analytics` (query: `site_id`, `period=daily|weekly|monthly`, `threshold_pct`)
- `GET /api/v1/consumer/anomalies` (query: `site_id`, `threshold_pct`)
- `GET /api/v1/consumer/benchmark` (query: `site_id`, `period=daily|weekly|monthly`)

### Account manager
- `GET /api/v1/account-manager/portfolio`
- `GET /api/v1/account-manager/alerts` (optional query: `status=open|acknowledged|resolved`)

### Exports
- `POST /api/v1/exports` (JSON body: `site_id`, `start_date`, `end_date`, `format=csv|pdf`)
- `GET /api/v1/exports/{job_id}/download` (query: `format`, `site_id`, `start_date`, `end_date`)

## Notes / next steps
- Replace placeholder/demo series with data sourced from PostgreSQL readings tables.
- Add baseline computation and anomaly detection logic, store anomalies/alerts, and implement status transitions.
- Replace placeholder PDF with real report generation.
