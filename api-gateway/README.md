# AM API Gateway v2.0

Central dynamic API Gateway for the **AM Asset Management** platform.

## Architecture

```
Client (Web / Mobile)
  │
  ▼
Traefik (edge, am-dev.asrax.in)
  │   path prefix: /am
  ▼
am-api-gateway  (this service)
  │  ┌──────────────────────────────────────────────────────┐
  │  │  SERVICES_REGISTRY in main.py                        │
  │  │                                                      │
  │  │  /am/trade/**        → am-trade-management-service   │
  │  │  /am/portfolio/**    → am-portfolio                  │
  │  │  /am/market-data/**  → am-market-data                │
  │  │  /am/subscriptions/**→ am-subscription               │
  │  │  /am/notifications/**→ am-notification               │
  │  │  /am/documents/**    → am-document-processor         │
  │  │  /am/analysis/**     → am-analysis                   │
  │  └──────────────────────────────────────────────────────┘
  │
  │  For each request, the gateway:
  │    1. Validates the user's Keycloak JWT (via auth service or edge headers)
  │    2. Exchanges user token for a service-scoped token
  │    3. Forwards request with X-User-ID + service token injected
  ▼
Downstream Microservice (trusts X-User-ID from gateway)
```

## Adding a New Service

Edit `SERVICES_REGISTRY` in [`main.py`](./main.py) — add ONE dict entry:

```python
"my-service": {
    "url": os.getenv("MY_SERVICE_URL", "http://my-service:8080"),
    "service_id": "my-service",
    "permissions": ["my-service:read", "my-service:write"],
},
```

Then add ONE env var to [`helm/values.yaml`](./helm/values.yaml):

```yaml
env:
  MY_SERVICE_URL: "http://my-service:8080"
```

**No new router file. No new import. Zero boilerplate.**

## Running Locally

```bash
pip install -r requirements.txt
AUTH_SERVICE_URL=http://localhost:8001 uvicorn main:app --reload --port 8080
```

Open: http://localhost:8080/docs

## Endpoints

| Path | Description |
|------|-------------|
| `GET /health` | Kubernetes liveness/readiness probe |
| `GET /` | Service info + registered services |
| `ANY /am/{service}/{path}` | Dynamic proxy to registered service |
| `GET /docs` | Interactive Swagger UI |

## Deployment (Dev)

```bash
helm upgrade --install am-api-gateway \
  oci://ghcr.io/am-portfolio/charts/universal-chart \
  -f helm/values.yaml \
  -f helm/values.dev.yaml \
  -f helm/vault-mappings.yaml \
  -n am-apps-dev
```
