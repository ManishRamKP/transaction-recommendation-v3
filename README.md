# finflow-transaction-recommendation-v3

**v3** of the Transaction Recommendation Engine — a major architectural upgrade that replaces AWS Athena with a direct PostgreSQL trade master query, adds GCP/BigQuery support, introduces S3 audit logging of every response, and expands trade metadata tracking.

---

## What's New in v3 (vs v2)

| Feature | v2 | v3 |
|---|---|---|
| Invoice data source | AWS Athena | PostgreSQL `trade_master` table (direct) |
| GCP / BigQuery support | ✗ | ✓ `trxn_reco_gcp.py` |
| S3 audit logging | ✗ | ✓ Every response saved to S3 |
| Trade metadata in response | ✗ | ✓ `trademaster_id`, `trademaster_trade_id`, `trade_ref_no` |
| Multi-source deduplication | Basic | ✓ TALLY → GDC → GST priority ranking |
| Environments | dev / uat / prod | dev / uat / prod / gcp |

---

## Files

| File | Environment | Data Source |
|---|---|---|
| `trxn_reco_dev.py` | Development | PostgreSQL (dev schema) |
| `trxn_reco_uat.py` | UAT | PostgreSQL (uat/prod schemas) |
| `trxn_reco_prod.py` | Production | PostgreSQL (prod schema) |
| `trxn_reco_gcp.py` | GCP | PostgreSQL + BigQuery + GCS |

---

## Prerequisites

- Python 3.8+
- Docker
- AWS credentials with S3 access
- PostgreSQL access (`cbapis` schema with `trade_master` table)
- GCP service account with BigQuery + GCS access (GCP variant only)
- Running CRE scoring API

---

## Configuration

All credentials come from config JSON files. **Never commit them with real values** — all four are in `.gitignore`.

### Environment variables

```bash
export CRE_API_URL=https://your-gateway/api/service/cre/compute_credit_score
export API_BEARER_TOKEN=your-bearer-token
export S3_AUDIT_BUCKET=your-audit-s3-bucket
export GCP_PROJECT_ID=your-gcp-project-id     # GCP variant only
```

### Config file structure

```json
{
  "aws": {
    "aws_access_key_id": "YOUR_AWS_ACCESS_KEY_ID",
    "aws_secret_access_key": "YOUR_AWS_SECRET_ACCESS_KEY",
    "region_name": "ap-south-1",
    "bucket_name": "YOUR_S3_BUCKET_NAME"
  },
  "athena": { "s3_output_location": "s3://YOUR_S3_BUCKET_NAME/" },
  "postgres": {
    "host": "YOUR_POSTGRES_HOST",
    "port": "5432",
    "db": "YOUR_DB",
    "user": "YOUR_USER",
    "password": "YOUR_PASSWORD"
  },
  "gcp": {
    "project_id": "YOUR_GCP_PROJECT_ID",
    "bucket_name": "YOUR_GCS_BUCKET_NAME",
    "credentials": { "...": "GCP service account JSON" }
  }
}
```

---

## Running Locally

```bash
git clone https://github.com/your-org/finflow-transaction-recommendation-v3.git
cd finflow-transaction-recommendation-v3

python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

export CRE_API_URL=https://your-gateway/compute_credit_score
export API_BEARER_TOKEN=your-token
export S3_AUDIT_BUCKET=your-bucket

python trxn_reco_dev.py      # dev
python trxn_reco_uat.py      # uat
python trxn_reco_prod.py     # prod
python trxn_reco_gcp.py      # gcp
```

Service runs on `http://localhost:8113`.

---

## Docker Deployment

```bash
docker build -t tre-v3 .
docker run -p 8113:8113 \
  -e CRE_API_URL=https://your-gateway/compute_credit_score \
  -e API_BEARER_TOKEN=your-token \
  -e S3_AUDIT_BUCKET=your-bucket \
  tre-v3
```

The Dockerfile defaults to `trxn_reco_gcp.py`. Edit `ENV FLASK_APP` for other environments.

---

## Kubernetes Deployment

Update image URIs in the deployment YAMLs then apply per environment:

```bash
# Dev
kubectl apply -f deployment_dev.yaml
kubectl apply -f service_dev.yaml

# UAT
kubectl apply -f deployment_uat.yaml
kubectl apply -f service_uat.yaml

# Prod
kubectl apply -f deployment_prod.yaml
kubectl apply -f service_prod.yaml
```

| Namespace | Type | Port |
|---|---|---|
| `finflow-tre-v3-dev` | ClusterIP | 8113 |
| `finflow-tre-v3-uat` | ClusterIP | 8113 |
| `finflow-tre-v3-prod` | ClusterIP | 8113 |

---

## API Reference

### `POST /transaction-recommendation`

Fetches candidate invoices from the `trade_master` PostgreSQL table, deduplicates against in-progress finance requests, scores each via CRE, and saves the full response to S3.

**Request**
```json
{
  "borrower_gst": "27XXXXXXXXXXXXX",
  "total_amount": 400000
}
```

**Response** includes `cumulative_assessment` (weighted trade score) and `individual_assessment` with `trademaster_id`, `trademaster_trade_id`, and `trade_ref_no` in addition to all v2 fields.

---

### `POST /transaction-recommendation-refine`

Scores a caller-supplied invoice list with additional document metrics. Now requires `trademaster_id`, `trademaster_trade_id`, and `trade_ref_no` as mandatory fields.

**Mandatory fields in `invoice_details`:**
`borrower_gst`, `trader_gst`, `current_invoice_amount`, `lgl_nm`, `invoice_id`, `invoice_date`, `trademaster_id`, `trademaster_trade_id`, `trade_ref_no`

---

## Invoice Source Priority (v3)

When the same invoice exists in multiple data sources, v3 picks based on this priority:

1. **TALLY** — highest trust
2. **GDC**
3. **GST**

Only the top-ranked record per invoice is recommended, preventing duplicates across sources.

---

## Audit Logging

Every API response is saved to S3 as a JSON file:
```
s3://{S3_AUDIT_BUCKET}/transaction_recommendation/{request_id}.json
```

---

## Related Services

- `finflow-cre-v11` — Core credit scoring engine
- `finflow-ekg-cre-metrics-v11` — Enterprise knowledge graph manager
- `finflow-transaction-recommendation-v1` — Original version
- `finflow-transaction-recommendation-v2` — Deduplication + multi-env
- `finflow-transaction-recommendation-v3` — This service

---

## Security Notes

- Never commit any `config_*.json` with real credentials — all four are in `.gitignore`
- Inject `API_BEARER_TOKEN` via environment variables or a secrets manager at runtime
- The Dockerfile copies `config_gcp.json` into the image — in production, mount credentials as a secret volume instead
- Rotate the Bearer token and GCP service account key regularly
