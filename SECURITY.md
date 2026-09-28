# Security Policy

## Current security posture (honest version)

agent-flow is a **local-first desktop application**. As of 0.1.0 it ships
**without authentication** on every entry point:

| Entry point | Auth | Binding |
|---|---|---|
| `POST /flows/{id}/run` | ❌ none | `127.0.0.1` only |
| `POST /flows/{id}/resume` (HITL approval) | ❌ none | `127.0.0.1` only |
| Chat share page (`public/chat.html`) | ❌ none | depends on bridge URL |
| MCP stdio servers | n/a | spawned locally |

Consequences you should understand before exposing anything beyond localhost:

1. **Anyone who can reach the port can run your flows and spend your LLM
   budget.** The bridge trusts its network unconditionally.
2. **`/resume` has no approver identity check.** Any reachable client can
   approve a pending human-in-the-loop step. Do not treat chat-page approvals
   as authenticated decisions.
3. **CORS is pinned to the local dev server origins** (`localhost:1420`) —
   this limits browser-based CSRF from arbitrary websites but is not an
   authentication mechanism.

**Rule of thumb: keep the bridge on `127.0.0.1`.** If you need remote access,
put an authenticating reverse proxy in front and treat the deployment as
out of scope for this repository's guarantees.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | ✅ security fixes only for the above-listed gaps, not new features |

## Reporting a vulnerability

Open a **private** GitHub security advisory (Repository → Security →
Report a vulnerability) rather than a public issue.

Please include: affected endpoint/component, reproduction steps, and impact
assessment. We aim to acknowledge reports within 7 days.

## Secrets handling

- LLM API keys are stored in the local SQLite database
  (`~/.agent-flow/agent_flow.db`, `kv['config']`) and never committed.
- Never paste keys into chat, screenshots, or shell history — prefer the
  in-app settings dialog.
- `.gitignore` excludes `.env`, `*.local`, runtime logs and PID files.
