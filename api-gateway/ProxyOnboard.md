# Vault Configuration & OIDC Integration Troubleshooting Guide (Dev Realm Migration)

This document provides a comprehensive post-mortem, troubleshooting guide, and step-by-step resolution path for migrating the developmental Kubernetes services (`am-apps-dev`) from the production Keycloak realm (`am-realm`) to the dedicated development realm (`am-dev-realm`).

---

## 1. Problem Overview & Root Cause Analysis

During developmental testing of the subscription endpoint (`https://am-dev.asrax.in/subscriptions/me`), requests failed with signature validation errors. Deep-dive inspection revealed four distinct systemic issues:

### Issue A: Hardcoded OIDC Values in Helm & Stale Pod State
- **Symptom**: Tokens issued via `/identity/auth/login` still verified against the production realm, and requests to microservices threw signature errors.
- **Problem**: 
  1. The base `values.yaml` in the Helm charts hardcoded the production issuer `http://auth.munish.org/auth/realms/am-realm`. Dev overrides were absent in `values.dev.yaml`.
  2. The `am-identity` pod was running continuously without restarts. Since the Uvicorn process parses environment variables at startup, any dynamic updates made by Vault Agent to `/vault/secrets/identity` on disk were ignored by the running Python process.
- **Solution**: Added OIDC dev realm environment overrides directly in `values.dev.yaml` and rollout-restarted the deployments to force a clean startup environment load.

### Issue B: Mismatched Client Secret in Vault
- **Symptom**: Token requests to `am-dev-realm` returned `invalid_client` or `unauthorized_client` errors.
- **Problem**: The Vault path `apps/data/dev/services/am-identity` still contained the stale/production client secret `CHG6b4LmR9yLOww8U9Vs0ewraIKOK7p4` instead of the newly generated dev secret `iio4Fbnpp14Rny3gZpQYIrcU7ra2ytfi`.
- **Solution**: Patched the Keycloak secret directly in Vault and rollout-restarted the dependent pods.

### Issue C: Self-Signed SSL Certificate Verification Failure
- **Symptom**: User registration `/auth/register` returned `500 Internal Server Error`. The logs contained `httpx.ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED]`.
- **Problem**: `KEYCLOAK_URL` in Vault was configured to use `https://auth.munish.org/auth`. The python containers do not trust the self-signed SSL certificate protecting the admin-cli endpoints.
- **Solution**: Patched `KEYCLOAK_URL` in Vault to use `http://auth.munish.org/auth`. Since the internal OIDC endpoints already use HTTP, this aligned all connections and bypassed SSL validation crashes seamlessly.

### Issue D: Symmetric HS256 vs. Asymmetric RS256 Token Mismatch
- **Symptom**: Even after successful login, requests to `/subscriptions/me` failed with `jwt.exceptions.PyJWKClientError: Unable to find a signing key that matches: "None"`.
- **Problem**: The API Gateway/Proxy (`am-asrax-proxy`) had a custom token minting feature enabled via `JWT_SECRET`. It converted Keycloak's `RS256` token into a symmetric `HS256` token for inter-service calls. Because `HS256` tokens do not contain a `kid` header, the downstream `am-subscription` service (using `am_platform_security` library) crashed trying to resolve a public key from Keycloak's JWKS.
- **Solution**: Removed/cleared the `JWT_SECRET` key in Vault under `apps/data/dev/services/am-asrax-proxy`. This triggered the gateway's fallback logic to safely forward the original user Keycloak RS256 token directly to downstream microservices.

---

## 2. Step-by-Step Resolution Walkthrough

Follow these steps to reproduce the diagnostic fixes and ensure all services are aligned in a dev or testing environment.

### Step 1: Align Local Secrets Configuration
Update [am-platform/.secrets.dev.env](file:///a:/InfraCode/AM-Portfolio-grp/am-platform/.secrets.dev.env) with HTTP schemes and correct secrets for the dev realm:
```ini
# KEYCLOAK ADMIN
KEYCLOAK_URL=http://auth.munish.org/auth
KEYCLOAK_REALM=am-dev-realm

# CONFIDENTIAL SERVICE CLIENTS
AM_IDENTITY_CLIENT_ID=am-identity-service
AM_IDENTITY_CLIENT_SECRET=iio4Fbnpp14Rny3gZpQYIrcU7ra2ytfi
```

### Step 2: Patch Vault Keycloak Secret Credentials
Update the parameters inside Vault's secret storage to matching dev parameters:
```bash
# Log in to the Vault server pod
kubectl exec -n vault vault-0 -it -- /bin/sh

# Patch the correct dev realm client secret and HTTP schema
vault kv patch apps/dev/services/am-identity \
  AM_IDENTITY_CLIENT_SECRET="iio4Fbnpp14Rny3gZpQYIrcU7ra2ytfi" \
  KEYCLOAK_URL="http://auth.munish.org/auth"
```

### Step 3: Configure Proxy Token Forwarding (Bypass Symmetric Minting)
Remove `JWT_SECRET` in the gateway/proxy configuration to enable transparent Keycloak bearer token pass-through:
```bash
# Clear JWT_SECRET to trigger fallback bearer token forwarding
vault kv patch apps/dev/services/am-asrax-proxy JWT_SECRET=""
```

### Step 4: Add Permanent OIDC Overrides in Helm Values
Modify [am-platform/am-subscription/helm/values.dev.yaml](file:///a:/InfraCode/AM-Portfolio-grp/am-platform/am-subscription/helm/values.dev.yaml) to ensure dev realm OIDC values are permanently set during deployment:
```yaml
env:
  APP_ENV: dev
  LOG_LEVEL: DEBUG
  OIDC_ISSUER: "http://auth.munish.org/auth/realms/am-dev-realm"
  OIDC_JWKS_URL: "http://auth.munish.org/auth/realms/am-dev-realm/protocol/openid-connect/certs"
  OIDC_AUDIENCE: "account"
```

### Step 5: Rollout Restart Services
Restart the microservices in the developmental namespace to load the fresh environment configurations:
```bash
# Restart the identity service
kubectl rollout restart deployment/am-identity -n am-apps-dev
kubectl rollout status deployment/am-identity -n am-apps-dev

# Restart the proxy gateway
kubectl rollout restart deployment/am-asrax-proxy -n am-apps-dev
kubectl rollout status deployment/am-asrax-proxy -n am-apps-dev
```

---

## 3. Post-Fix Verification Routine

Use the following sequence to test that the authentication pipeline is working correctly.

### 1. Register a test user in Keycloak dev realm
Verify that SSL certificate exceptions are gone:
```powershell
$body = @{
    email = "test.user@example.com"
    first_name = "Test"
    last_name = "User"
    password = "TestPass123!"
} | ConvertTo-Json
Invoke-RestMethod -Uri "https://am-dev.asrax.in/identity/auth/register" -Method Post -Body $body -ContentType "application/json"
```
**Expected Response**: `status: created`

### 2. Login to acquire a fresh JWT
Verify that the generated token belongs to the dev realm `am-dev-realm`:
```powershell
$body = @{ username = "test.user@example.com"; password = "TestPass123!" } | ConvertTo-Json
$resp = Invoke-RestMethod -Uri "https://am-dev.asrax.in/identity/auth/login" -Method Post -Body $body -ContentType "application/json"
Write-Host "Dev Realm JWT Token Acquired!"
```

### 3. Retrieve Subscription Context End-to-End
Query the subscription endpoint using the newly minted JWT:
```powershell
Invoke-RestMethod -Uri "https://am-dev.asrax.in/subscriptions/me" -Headers @{ Authorization = "Bearer $($resp.access_token)" }
```
**Expected Response**: Returns a valid `200 OK` JSON containing the active subscription details matching the `am_free` plan limits.

---

## 4. Preproduction Ingress Routing Troubleshooting Guide (503 Service Unavailable)

### Problem Description
When making request `POST https://am.asrax.in/identity/auth/login` to the preprod login endpoint, Traefik returned `503 Service Unavailable` with `no available server` (or similar internal gateway failure). 

### Root Cause Analysis
1. **Host-Rule Collisions**: Both `am-apps-preprod` and `am-apps-prod` namespaces had separate, active ingress resources (`am-identity`, `am-subscription`, `am-notification`) configured with the exact same hostname `am.asrax.in` and respective path prefixes (e.g. `/identity`).
2. **Endpoint Starvation**: Since `am-identity` in the `am-apps-prod` namespace was scaled down to `0/2` replicas (since production is not currently active), Traefik's merged routing table directed the external requests to the production backend, which had zero available endpoints, resulting in a `503` error.

### Resolution Steps
1. **Identify and Validate Colliding Ingress Resources**: Checked all active ingress rules across namespaces using `kubectl get ingress -A`. Discovered the duplicates in `am-apps-prod`.
2. **Remove Stale Production Ingress Mappings**: Since the production namespace is not actively serving traffic and has its pods scaled to `0`, deleted the stale colliding ingresses in `am-apps-prod` using the following:
   ```bash
   kubectl --kubeconfig="VPS\kubeconfig.vps" delete ingress am-identity -n am-apps-prod
   kubectl --kubeconfig="VPS\kubeconfig.vps" delete ingress am-subscription -n am-apps-prod
   kubectl --kubeconfig="VPS\kubeconfig.vps" delete ingress am-notification -n am-apps-prod
   ```
3. **Verify Traefik Auto-Reload & Healthy Ingress Routing**: Traefik automatically purged the colliding production router. All `/identity`, `/subscriptions`, and `/notifications` routing rules on the domain `am.asrax.in` now cleanly point exclusively to the active `am-apps-preprod` namespace backends (which are scaled to `1` replica and fully healthy).

### Post-Fix Verification Routine
Verify the login pipeline end-to-end on preproduction:
```bash
curl.exe -i -X POST "https://am.asrax.in/identity/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username": "test.user@example.com", "password": "TestPass123!"}'
```
**Expected Response**: `200 OK` along with a valid JWT token issued by the production/preproduction Keycloak instance:
```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIs...",
  "expires_in": 300,
  "refresh_token": "eyJhbGciOiJIUzUxMiIs...",
  "token_type": "Bearer"
}
```

---

## 5. Preproduction Gateway Proxy / Subscriptions Routing Alignment (Dev Parity)

### Problem Description
To achieve parity with the dev setup, the subscription path `GET https://am.asrax.in/subscriptions/subscriptions/me` must route through the API Gateway (`am-asrax-proxy`) for rate limiting, security headers validation, and inter-service token validation, rather than hitting the `am-subscription` service directly.

### Root Cause Analysis & Typo Discovery
1. **Direct Ingress Mapping**: The `am-subscription` Ingress in `am-apps-preprod` directly routed the `/subscriptions` path prefix, thereby bypassing the gateway proxy entirely.
2. **Broken Proxy Ingress**: Even after adding `/subscriptions` to `am-asrax-proxy`'s ingress resource, Traefik was throwing routing configuration errors:
   `error="middleware \"am-apps-preprod-preprod-global-cors@kubernetescrd\" does not exist"`
   This was due to a double-prefix typo (`-preprod-preprod-`) in `am-asrax-proxy/helm/values.preprod.yaml`'s ingress middleware annotation. Because of this, Traefik silently discarded all routes created from the proxy ingress.
3. **Vault Mismatched JWT Secret (Issue D Parity)**: Once the proxy intercepted the request, it returned `500 Internal Server Error` because `am-asrax-proxy` had `JWT_SECRET` configured in Vault under the path `apps/preprod/services/am-asrax-proxy`. This triggered symmetric `HS256` token translation, stripping the `kid` header and crashing the downstream subscription service.

### Resolution Steps
1. **Fix Ingress Typo**: Corrected `am-apps-preprod-preprod-global-cors` to `am-apps-preprod-global-cors` in the Helm values and patched the live Ingress annotation in the cluster.
2. **Align Ingress Routes**: 
   - Patched `am-subscription` Ingress in `am-apps-preprod` to only expose `/webhooks/provider` directly.
   - Patched `am-asrax-proxy` Ingress in `am-apps-preprod` to route `/subscriptions` prefix to the proxy.
3. **Clear Vault Token Secret**: Removed the `JWT_SECRET` key from the Vault secret storage `apps/preprod/services/am-asrax-proxy` to enable standard Keycloak asymmetric token pass-through:
   ```bash
   kubectl exec -n vault vault-0 -it -- vault kv patch apps/preprod/services/am-asrax-proxy JWT_SECRET=""
   ```
4. **Trigger Deployment Restart**: Rollout-restarted `am-asrax-proxy` in `am-apps-preprod` to pick up the cleared secret configuration.

### Post-Fix Verification Routine
Verify the subscription route end-to-end through the proxy on preproduction:
```bash
curl.exe -i -X GET "https://am.asrax.in/subscriptions/subscriptions/me" \
  -H "Authorization: Bearer <valid_token>"
```
**Expected Response**: `200 OK` with the rate limiting and security headers injected by `am-asrax-proxy`:
```http
HTTP/1.1 200 OK
x-frame-options: DENY
x-ratelimit-limit: 100
x-ratelimit-remaining: 97
...
{"data":{"plan_code":"am_free","state":"active",...}}
```
