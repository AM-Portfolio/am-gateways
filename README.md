# am-gateways

Monorepo for L2 edge services. Folder name ≠ K8s/image name.

| Deploy / image | Folder | Purpose |
|----------------|--------|---------|
| **am-api-gateway** | `api-gateway/` | Product REST edge — does not replace legacy `am-asrax-proxy` |
| **am-ai-gateway** | `mcp-gateway/` | AI chat edge → finance agent |

## Layout

```
am-gateways/
  .github/workflows/     # am-pipelines central-build-publish / central-deploy
  api-gateway/           # product edge + helm values
  mcp-gateway/           # AI proxy + Dockerfile + helm (chart: am-ai-gateway)
```

## Do not confuse with

- Finance L3 agent: **`am-agents/fin-portfolio-agent`** (not here).
- Product screens → **am-api-gateway** (`api-gateway/`).
- AI chat → **am-ai-gateway** (`mcp-gateway/`) → `fin-portfolio-agent`.

## Local run (AI path)

```powershell
# Terminal 1 — finance agent (am-agents)
cd ..\am-agents\fin-portfolio-agent
# start on port 8101 → POST /api/v1/ai/chat

# Terminal 2 — AI gateway
cd mcp-gateway
pip install -r requirements.txt
$env:FINANCE_AGENT_BASE_URL = "http://localhost:8101"
uvicorn app.main:app --host 0.0.0.0 --port 8120
```

UI (`am-modern-ui`) prefers `services.aiGateway` → `http://localhost:8120`.

Helm: `mcp-gateway/helm/` (release name **am-ai-gateway**).  
CI: root `.github` → `AM-Portfolio/am-pipelines`.  
Workspace note: `../AI_CHAT.md`.

## Branch

`feature/ai-chat-l3`
