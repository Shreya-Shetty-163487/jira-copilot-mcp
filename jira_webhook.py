import os, json, logging, re, asyncio, base64
from contextlib import asynccontextmanager
from typing import Any

import requests as http_requests
from dotenv import load_dotenv
load_dotenv()

import uvicorn
from fastapi import FastAPI, Request, HTTPException, Header, BackgroundTasks
from pyngrok import ngrok, conf
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ngrok / webhook
# ---------------------------------------------------------------------------
NGROK_AUTH_TOKEN     = os.getenv("NGROK_AUTH_TOKEN")
JIRA_WEBHOOK_SECRET  = os.getenv("JIRA_WEBHOOK_SECRET", "")
APP_PORT             = int(os.getenv("PORT", "8000"))

# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------
GITHUB_TOKEN  = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO   = os.getenv("GITHUB_REPO", "")   # owner/repo, e.g. Shreya-Shetty-163487/jira-copilot-mcp
GITHUB_API    = "https://api.github.com"
COPILOT_ASSIGNEE = "copilot-swe-agent[bot]"
JIRA_LINK_MARKER = "<!-- jira-key:"

# Track PRs we've already posted Jira comments for (avoids duplicates across opened/merged)
_commented_prs: set[str] = set()

# ---------------------------------------------------------------------------
# Ticket quality check (GitHub Models / GPT-4o)
# ---------------------------------------------------------------------------
GITHUB_MODELS_ENDPOINT = "https://models.inference.ai.azure.com/chat/completions"
GITHUB_MODELS_MODEL    = "gpt-4o"
QUALITY_PASS_SCORE     = 6

# ---------------------------------------------------------------------------
# Atlassian MCP (Basic auth via API token)
# ---------------------------------------------------------------------------
JIRA_EMAIL         = os.getenv("JIRA_EMAIL", "")
MCP_API_TOKEN      = os.getenv("MCP_API_TOKEN", "")
ATLASSIAN_CLOUD_ID = os.getenv("ATLASSIAN_CLOUD_ID", "").strip("'\"")
MCP_URL = "https://mcp.atlassian.com/v1/mcp/authv2"


# ---------------------------------------------------------------------------
# ngrok lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    if NGROK_AUTH_TOKEN:
        conf.get_default().auth_token = NGROK_AUTH_TOKEN
    tunnel = ngrok.connect(APP_PORT, "http")
    public_url: str = tunnel.public_url
    if public_url.startswith("http://"):
        public_url = public_url.replace("http://", "https://", 1)
    logger.info("=" * 60)
    logger.info("ngrok public URL   : %s", public_url)
    logger.info("Jira webhook URL   : %s/jira/webhook", public_url)
    logger.info("GitHub webhook URL : %s/github/webhook", public_url)
    logger.info("=" * 60)
    yield
    logger.info("Shutting down ngrok tunnel...")
    ngrok.disconnect(tunnel.public_url)
    ngrok.kill()


app = FastAPI(title="Jira Webhook Receiver", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Jira webhook → create GitHub Issue assigned to @copilot
# ---------------------------------------------------------------------------
@app.post("/jira/webhook")
async def jira_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    if JIRA_WEBHOOK_SECRET:
        import hmac, hashlib
        raw_body = await request.body()
        expected = hmac.new(JIRA_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
        signature = (x_hub_signature or "").removeprefix("sha256=")
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        payload: dict[str, Any] = await request.json()
    else:
        payload = await request.json()

    event: str = payload.get("webhookEvent", "unknown")
    logger.info("Received Jira webhook event: %s", event)

    issue = payload.get("issue", {})
    key   = issue.get("key", "?")

    if event in ("jira:issue_created", "jira:issue_updated"):
        summary = issue.get("fields", {}).get("summary", "")
        description = issue.get("fields", {}).get("description", "") or ""
        logger.info("[%s] %s - %s", "CREATED" if "created" in event else "UPDATED", key, summary)
        background_tasks.add_task(_create_github_issue, key, summary, description)
    elif event == "jira:issue_deleted":
        logger.info("[DELETED] %s", key)
    else:
        logger.info("Unhandled event: %s", event)

    return {"received": True, "event": event}


# ---------------------------------------------------------------------------
# GitHub webhook → PR opened by Copilot → post Jira comment
# ---------------------------------------------------------------------------
@app.post("/github/webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    payload: dict[str, Any] = await request.json()
    action = payload.get("action", "")

    # We only care about pull_request events with action "opened"
    pr = payload.get("pull_request")
    if not pr:
        return {"received": True, "skipped": "not a pull_request event"}

    if action not in ("opened", "ready_for_review", "closed"):
        logger.info("[GitHub] PR action=%s -- ignoring", action)
        return {"received": True, "skipped": f"action={action}"}

    # For 'closed', only act if the PR was merged
    if action == "closed" and not pr.get("merged", False):
        logger.info("[GitHub] PR closed without merge -- ignoring")
        return {"received": True, "skipped": "closed without merge"}

    pr_url   = pr.get("html_url", "")
    pr_title = pr.get("title", "")
    pr_body  = pr.get("body", "") or ""
    pr_user  = pr.get("user", {}).get("login", "")
    pr_num   = pr.get("number", "?")
    pr_branch = pr.get("head", {}).get("ref", "")

    logger.info("[GitHub] PR #%s %s by %s: %s", pr_num, "merged" if action == "closed" else action, pr_user, pr_title)

    # Extract Jira ticket key from PR title, body, or branch name (e.g., PROJ-123)
    jira_key = _extract_jira_key(pr_title) or _extract_jira_key(pr_body) or _extract_jira_key(pr_branch.upper())

    # If not in PR title/body/branch, check linked GitHub issues for the Jira key
    if not jira_key:
        jira_key = _find_jira_key_from_linked_issues(pr_body, pr_num)

    if not jira_key:
        logger.warning("[GitHub] No Jira ticket key found in PR #%s title/body/branch/linked issues -- skipping", pr_num)
        return {"received": True, "skipped": "no Jira key found"}

    logger.info("[GitHub] Found Jira key %s in PR #%s -- posting comment", jira_key, pr_num)

    # Deduplicate: don't post twice for the same PR (opened + merged)
    dedup_key = f"{jira_key}:{pr_num}"
    if dedup_key in _commented_prs:
        logger.info("[GitHub] Already posted comment for %s PR #%s -- skipping", jira_key, pr_num)
        return {"received": True, "skipped": "already commented"}
    _commented_prs.add(dedup_key)

    background_tasks.add_task(_post_jira_comment, jira_key, pr_url, pr_title, pr_user)

    return {"received": True, "jira_key": jira_key, "pr": pr_num}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def _extract_jira_key(text: str) -> str | None:
    m = _JIRA_KEY_RE.search(text)
    return m.group(1) if m else None


def _github_headers() -> dict[str, str]:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


def _find_jira_key_from_linked_issues(pr_body: str, pr_num: Any) -> str | None:
    """Search linked GitHub issues for a Jira key marker or Jira key pattern."""
    # Find issue references like #10, Fixes #10, Closes #10
    issue_refs = re.findall(r"#(\d+)", pr_body)
    if not issue_refs:
        # Also try the PR's linked issues via the timeline/events API
        try:
            resp = http_requests.get(
                f"{GITHUB_API}/repos/{GITHUB_REPO}/pulls/{pr_num}",
                headers=_github_headers(),
            )
            if resp.ok:
                pr_data = resp.json()
                body = pr_data.get("body", "") or ""
                issue_refs = re.findall(r"#(\d+)", body)
        except Exception:
            pass

    for issue_num in issue_refs:
        try:
            resp = http_requests.get(
                f"{GITHUB_API}/repos/{GITHUB_REPO}/issues/{issue_num}",
                headers=_github_headers(),
            )
            if not resp.ok:
                continue
            issue_data = resp.json()
            # Check title and body for Jira key
            title = issue_data.get("title", "")
            body = issue_data.get("body", "") or ""
            key = _extract_jira_key(title) or _extract_jira_key(body)
            if key:
                logger.info("[GitHub] Found Jira key %s from linked issue #%s", key, issue_num)
                return key
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Ticket quality validation via GitHub Models (GPT-4o)
# ---------------------------------------------------------------------------
def _validate_ticket_quality(ticket_key: str, summary: str, description: str) -> dict[str, Any]:
    """Score the Jira ticket for clarity/completeness. Returns quality dict."""
    prompt = f"""You are a senior engineering manager reviewing a Jira ticket before it gets assigned to an AI coding agent.
Score the ticket strictly from 0 to 10 based on how actionable and unambiguous it is for an AI to implement without any human follow-up.

Scoring criteria:
- Title clarity (is it specific, not vague like 'fix bug') [0-2]
- Description completeness (what to build, not just what's wrong) [0-2]
- Acceptance criteria present and testable [0-2]
- Edge cases or error scenarios mentioned [0-2]
- No ambiguous words like 'improve', 'enhance', 'maybe', 'somehow' [0-2]

JIRA TICKET:
Title: {summary}
Description: {description or '(empty)'}

Respond with ONLY valid JSON — no explanation, no markdown fences.
{{{{
  "score": <integer 0-10>,
  "dimension_scores": {{{{
    "title_clarity": <0-2>,
    "description_completeness": <0-2>,
    "acceptance_criteria": <0-2>,
    "edge_cases": <0-2>,
    "no_ambiguity": <0-2>
  }}}},
  "missing": ["<section completely absent>"],
  "improvements": ["<specific problem with current content>"],
  "rewritten_title": "<actionable rewrite of the title>",
  "rewritten_description": "<2-3 sentence rewrite stating exactly what to build>",
  "example_acceptance_criteria": [
    "<testable criterion 1>",
    "<testable criterion 2>"
  ],
  "example_edge_cases": [
    "<edge case 1>",
    "<edge case 2>"
  ]
}}}}"""

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GITHUB_MODELS_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }

    try:
        resp = http_requests.post(GITHUB_MODELS_ENDPOINT, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
    except http_requests.RequestException as exc:
        logger.error("[%s] Quality check API failed: %s", ticket_key, exc)
        return {"score": QUALITY_PASS_SCORE, "skipped": True}  # pass through on failure

    raw = resp.json()["choices"][0]["message"]["content"] or ""
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("```", 2)[1]
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.rsplit("```", 1)[0].strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        logger.error("[%s] Quality check returned invalid JSON: %s", ticket_key, raw[:200])
        return {"score": QUALITY_PASS_SCORE, "skipped": True}


# ---------------------------------------------------------------------------
# Create a GitHub Issue and assign to Copilot Coding Agent
# ---------------------------------------------------------------------------
async def _create_github_issue(ticket_key: str, summary: str, description: str) -> None:
    if not GITHUB_TOKEN or not GITHUB_REPO:
        logger.warning("[%s] GITHUB_TOKEN or GITHUB_REPO not set — skipping", ticket_key)
        return

    # --- Ticket quality gate ---
    quality = _validate_ticket_quality(ticket_key, summary, description)
    score = int(quality.get("score", 0))
    logger.info("[%s] Ticket quality score: %d/10", ticket_key, score)

    quality_section = ""
    if score < QUALITY_PASS_SCORE:
        logger.warning("[%s] Low quality score (%d < %d) — creating issue with warnings", ticket_key, score, QUALITY_PASS_SCORE)
        missing = quality.get("missing", [])
        improvements = quality.get("improvements", [])
        quality_section = (
            f"\n\n> **WARNING: Ticket Quality (score: {score}/10)**\n"
            f"> Missing: {', '.join(missing) if missing else 'N/A'}\n"
            f"> Improvements: {', '.join(improvements) if improvements else 'N/A'}\n"
            f"> Suggested title: {quality.get('rewritten_title', 'N/A')}\n"
            f"> Suggested description: {quality.get('rewritten_description', 'N/A')}\n"
        )

    jira_base_url = os.getenv("JIRA_BASE_URL", "").strip("'\"")
    jira_url = f"{jira_base_url}/browse/{ticket_key}" if jira_base_url else ticket_key

    title = f"[{ticket_key}] {summary}"
    body = (
        f"## {summary}\n\n"
        f"**Description:**\n{description}\n"
        f"{quality_section}\n"
        f"## Definition of done\n\n"
        f"- [ ] Code implemented\n"
        f"- [ ] Tests written and passing\n"
        f"- [ ] No breaking changes to existing endpoints/pages\n\n"
        f"---\n"
        f"**Jira ticket:** [{ticket_key}]({jira_url})\n"
        f"{JIRA_LINK_MARKER} {ticket_key} -->\n"
        f"_Auto-created from Jira webhook._"
    )

    resp = http_requests.post(
        f"{GITHUB_API}/repos/{GITHUB_REPO}/issues",
        headers=_github_headers(),
        json={"title": title, "body": body},
    )

    if not resp.ok:
        logger.error(
            "[%s] Failed to create GitHub Issue: %s %s — %s",
            ticket_key, resp.status_code, resp.reason, resp.text[:300],
        )
        return

    issue_data = resp.json()
    issue_number = issue_data.get("number")
    logger.info(
        "[%s] GitHub Issue #%s created: %s",
        ticket_key, issue_number, issue_data.get("html_url"),
    )

    # --- Assign to Copilot Coding Agent ---
    _assign_copilot(ticket_key, issue_number)


# ---------------------------------------------------------------------------
# Assign to Copilot Coding Agent (with fallback to @copilot comment)
# ---------------------------------------------------------------------------
def _assign_copilot(ticket_key: str, issue_number: int) -> None:
    """Try assignee API with copilot-swe-agent[bot], fall back to @copilot comment."""
    # Attempt 1: Assign via API
    assign_resp = http_requests.post(
        f"{GITHUB_API}/repos/{GITHUB_REPO}/issues/{issue_number}/assignees",
        headers=_github_headers(),
        json={"assignees": [COPILOT_ASSIGNEE]},
    )
    if assign_resp.ok:
        assignees = [a.get("login", "") for a in assign_resp.json().get("assignees", [])]
        if any("copilot" in a.lower() for a in assignees):
            logger.info("[%s] Assigned %s to Issue #%s", ticket_key, COPILOT_ASSIGNEE, issue_number)
            return

    logger.warning("[%s] Assignee API didn't stick, falling back to @copilot comment", ticket_key)

    # Attempt 2: Trigger via @copilot comment
    comment_resp = http_requests.post(
        f"{GITHUB_API}/repos/{GITHUB_REPO}/issues/{issue_number}/comments",
        headers=_github_headers(),
        json={"body": "@copilot implement this issue."},
    )
    if comment_resp.ok:
        logger.info("[%s] Triggered @copilot via comment on Issue #%s", ticket_key, issue_number)
    else:
        logger.error("[%s] Failed to trigger @copilot: %s", ticket_key, comment_resp.text[:200])


# ---------------------------------------------------------------------------
# Atlassian MCP auth helper
# ---------------------------------------------------------------------------
def _mcp_auth_headers() -> dict[str, str]:
    """Build Basic auth headers for the Atlassian MCP server."""
    creds = base64.b64encode(f"{JIRA_EMAIL}:{MCP_API_TOKEN}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


# ---------------------------------------------------------------------------
# MCP helper — call Atlassian MCP tool with retry
# ---------------------------------------------------------------------------
async def _call_mcp_tool(session: ClientSession, name: str, args: dict[str, Any], ticket_key: str) -> str:
    last_error = ""
    for attempt in range(1, 6):
        result = await session.call_tool(name, arguments=args)
        text_parts = [c.text for c in result.content if hasattr(c, "text")]
        text = "\n".join(text_parts) if text_parts else json.dumps({"result": "ok"})
        if '"error":true' in text and "try again" in text.lower() and attempt < 5:
            logger.warning("[%s] MCP tool %s transient error (attempt %d/5), retrying...", ticket_key, name, attempt)
            await asyncio.sleep(3 * attempt)
            last_error = text
            continue
        return text
    return last_error


# ---------------------------------------------------------------------------
# Post a comment on Jira when Copilot opens a PR
# ---------------------------------------------------------------------------
async def _post_jira_comment(jira_key: str, pr_url: str, pr_title: str, pr_user: str) -> None:
    comment_body = (
        f"A pull request has been opened by *{pr_user}*:\n\n"
        f"*[{pr_title}|{pr_url}]*\n\n"
        f"PR Link: {pr_url}"
    )

    try:
        async with streamablehttp_client(MCP_URL, headers=_mcp_auth_headers()) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await _call_mcp_tool(
                    session,
                    "addCommentToJiraIssue",
                    {
                        "issueIdOrKey": jira_key,
                        "cloudId": ATLASSIAN_CLOUD_ID,
                        "commentBody": comment_body,
                        "contentFormat": "markdown",
                    },
                    jira_key,
                )
                logger.info("[%s] Jira comment result: %s", jira_key, result[:200])
    except Exception as exc:
        logger.error("[%s] Failed to post Jira comment via MCP: %s", jira_key, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run("jira_webhook:app", host="0.0.0.0", port=APP_PORT, reload=False)
