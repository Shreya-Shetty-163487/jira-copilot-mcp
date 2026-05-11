import os, json, logging, subprocess, asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import requests as http_requests
from dotenv import load_dotenv
load_dotenv()

import uvicorn
from fastapi import FastAPI, Request, HTTPException, Header, BackgroundTasks
from openai import AsyncOpenAI
from pyngrok import ngrok, conf
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ngrok / webhook
# ---------------------------------------------------------------------------
NGROK_AUTH_TOKEN   = os.getenv("NGROK_AUTH_TOKEN")
JIRA_WEBHOOK_SECRET = os.getenv("JIRA_WEBHOOK_SECRET", "")
APP_PORT           = int(os.getenv("PORT", "8000"))

# ---------------------------------------------------------------------------
# GitHub Models - gpt-4o
# ---------------------------------------------------------------------------
GITHUB_TOKEN           = os.getenv("GITHUB_TOKEN", "")
GITHUB_MODELS_ENDPOINT = "https://models.inference.ai.azure.com"
MODEL_ID               = "gpt-4o"

# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------
WORKSPACE_ROOT  = Path(os.getenv("WORKSPACE_ROOT", Path(__file__).parent))
SKIP_DIRS       = {".git", ".venv", "venv", "__pycache__", "node_modules", ".vscode"}
SKIP_FILES      = {"jira_webhook.py", "atlassian_auth.py", ".env"}
MAX_READ_CHARS  = 8000

# ---------------------------------------------------------------------------
# Atlassian OAuth2 (for MCP server auth)
# ---------------------------------------------------------------------------
ATLASSIAN_CLIENT_ID     = os.getenv("ATLASSIAN_CLIENT_ID", "")
ATLASSIAN_CLIENT_SECRET = os.getenv("ATLASSIAN_CLIENT_SECRET", "")
ATLASSIAN_REFRESH_TOKEN = os.getenv("ATLASSIAN_REFRESH_TOKEN", "")
ATLASSIAN_CLOUD_ID      = os.getenv("ATLASSIAN_CLOUD_ID", "")
MCP_URL = "https://mcp.atlassian.com/v1/mcp"

# ---------------------------------------------------------------------------
# Local tool definitions (file access + git - not in Atlassian MCP)
# ---------------------------------------------------------------------------
LOCAL_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all files in the workspace. Use this first to discover what exists before reading.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subdir": {"type": "string", "description": "Optional subdirectory to list. Leave empty for root."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full content of a specific workspace file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path, e.g. app.py or templates/index.html"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_files",
            "description": (
                "Write code changes to disk. Provide every file that must be "
                "created or modified with its FULL content (not a diff)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path":    {"type": "string", "description": "Workspace-relative path, e.g. app.py"},
                                "content": {"type": "string", "description": "Full file content"},
                            },
                            "required": ["path", "content"],
                        },
                    },
                    "summary": {"type": "string", "description": "One-sentence summary of all changes"},
                },
                "required": ["files", "summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_git_branch",
            "description": "Create a new git branch from current HEAD and push it to remote origin.",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch_name": {"type": "string", "description": "e.g. feature/TA-14-add-login"},
                },
                "required": ["branch_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commit_and_push",
            "description": "Stage all modified files, create a git commit, and push to the current remote branch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Git commit message"},
                },
                "required": ["message"],
            },
        },
    },
]


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
    logger.info("ngrok public URL : %s", public_url)
    logger.info("Jira webhook URL : %s/jira/webhook", public_url)
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
# Jira webhook endpoint
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

    if event == "jira:issue_created":
        logger.info("[CREATED] %s - %s", key, issue.get("fields", {}).get("summary", ""))
        background_tasks.add_task(_run_agentic_loop, key)
    elif event == "jira:issue_updated":
        changed = [i.get("field") for i in payload.get("changelog", {}).get("items", [])]
        logger.info("[UPDATED] %s - changed fields: %s", key, changed)
        background_tasks.add_task(_run_agentic_loop, key)
    elif event == "jira:issue_deleted":
        logger.info("[DELETED] %s", key)
    else:
        logger.info("Unhandled event: %s", event)

    return {"received": True, "event": event}


# ---------------------------------------------------------------------------
# Workspace helpers (used by list_files / read_file tools)
# ---------------------------------------------------------------------------
def _tool_list_files(subdir: str = "") -> str:
    base = (WORKSPACE_ROOT / subdir) if subdir else WORKSPACE_ROOT
    files: list[str] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(WORKSPACE_ROOT)
        if any(p in SKIP_DIRS for p in relative.parts):
            continue
        if path.name in SKIP_FILES:
            continue
        files.append(relative.as_posix())
    return json.dumps({"files": files})


def _tool_read_file(rel_path: str) -> str:
    path = WORKSPACE_ROOT / rel_path.strip()
    if path.name in SKIP_FILES:
        return json.dumps({"error": f"Access to {path.name} is not permitted."})
    if not path.is_file():
        return json.dumps({"error": f"File not found: {rel_path}"})
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return json.dumps({"error": str(exc)})
    if len(content) > MAX_READ_CHARS:
        content = content[:MAX_READ_CHARS] + "\n... [truncated — file too large]"
    return json.dumps({"path": rel_path, "content": content})


# ---------------------------------------------------------------------------
# Atlassian OAuth2 - exchange refresh token for access token
# ---------------------------------------------------------------------------
def _get_atlassian_access_token() -> str:
    global ATLASSIAN_REFRESH_TOKEN
    if not ATLASSIAN_REFRESH_TOKEN:
        raise RuntimeError(
            "ATLASSIAN_REFRESH_TOKEN not set in .env. "
            "Run python atlassian_auth.py to complete the OAuth flow."
        )
    resp = http_requests.post(
        "https://auth.atlassian.com/oauth/token",
        json={
            "grant_type":    "refresh_token",
            "client_id":     ATLASSIAN_CLIENT_ID,
            "client_secret": ATLASSIAN_CLIENT_SECRET,
            "refresh_token": ATLASSIAN_REFRESH_TOKEN,
        },
    )
    if not resp.ok:
        raise RuntimeError(
            f"{resp.status_code} {resp.reason} — {resp.text}"
        )
    data = resp.json()
    access_token = data.get("access_token", "")
    if not access_token:
        raise RuntimeError(f"No access_token in Atlassian token response: {data}")

    # Atlassian issues a new refresh token on each exchange (single-use rotation).
    # Persist the new one so the next request doesn't fail with "invalid refresh_token".
    new_refresh = data.get("refresh_token", "")
    if new_refresh and new_refresh != ATLASSIAN_REFRESH_TOKEN:
        ATLASSIAN_REFRESH_TOKEN = new_refresh
        _update_env_file("ATLASSIAN_REFRESH_TOKEN", new_refresh)
        logger.info("[OAuth] Refresh token rotated and saved to .env.")

    logger.info("[OAuth] Access token refreshed successfully.")
    return access_token


def _update_env_file(key: str, value: str) -> None:
    env_path = Path(__file__).parent / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
        updated = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                lines[i] = f"{key}={value}"
                updated = True
                break
        if not updated:
            lines.append(f"{key}={value}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("[OAuth] Could not update .env: %s", exc)


# ---------------------------------------------------------------------------
# MCP helper
# ---------------------------------------------------------------------------
def _mcp_tool_to_openai(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name":        tool.name,
            "description": tool.description or "",
            "parameters":  tool.inputSchema,
        },
    }


async def _call_mcp_tool(session: ClientSession, name: str, args: dict[str, Any], ticket_key: str) -> str:
    last_error = ""
    for attempt in range(1, 4):
        result = await session.call_tool(name, arguments=args)
        text_parts = [c.text for c in result.content if hasattr(c, "text")]
        text = "\n".join(text_parts) if text_parts else json.dumps({"result": "ok"})
        # Check for transient Atlassian errors and retry
        if '"error":true' in text and "try again" in text.lower() and attempt < 3:
            logger.warning("[%s] MCP tool %s transient error (attempt %d/3), retrying...", ticket_key, name, attempt)
            await asyncio.sleep(2 * attempt)
            last_error = text
            continue
        return text
    return last_error


# ---------------------------------------------------------------------------
# Local tool executors (git + file writing)
# ---------------------------------------------------------------------------
def _tool_write_files(files: list[dict[str, str]], summary: str, ticket_key: str) -> str:
    written: list[str] = []
    for f in files:
        rel_path = f.get("path", "").strip()
        content  = f.get("content", "")
        if not rel_path:
            continue
        target = WORKSPACE_ROOT / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(rel_path)
        logger.info("[%s] Written: %s", ticket_key, rel_path)
    logger.info("[%s] write_files summary: %s", ticket_key, summary)
    return json.dumps({"written": written, "summary": summary})


def _tool_create_git_branch(branch_name: str) -> str:
    try:
        subprocess.run(["git", "checkout", "-b", branch_name], cwd=WORKSPACE_ROOT, check=True, capture_output=True)
        push = subprocess.run(["git", "push", "-u", "origin", branch_name], cwd=WORKSPACE_ROOT, capture_output=True, text=True)
        pushed = push.returncode == 0
        return json.dumps({"branch": branch_name, "pushed": pushed, "note": push.stderr.strip() if not pushed else ""})
    except subprocess.CalledProcessError as exc:
        return json.dumps({"error": exc.stderr.decode() if exc.stderr else str(exc)})


def _tool_commit_and_push(message: str) -> str:
    try:
        subprocess.run(["git", "add", "-A"], cwd=WORKSPACE_ROOT, check=True, capture_output=True)
        commit = subprocess.run(["git", "commit", "-m", message], cwd=WORKSPACE_ROOT, capture_output=True, text=True)
        if commit.returncode != 0:
            return json.dumps({"error": commit.stderr.strip() or commit.stdout.strip()})
        push = subprocess.run(["git", "push"], cwd=WORKSPACE_ROOT, capture_output=True, text=True)
        pushed = push.returncode == 0
        return json.dumps({"committed": True, "pushed": pushed, "note": push.stderr.strip() if not pushed else ""})
    except subprocess.CalledProcessError as exc:
        return json.dumps({"error": exc.stderr.decode() if exc.stderr else str(exc)})


def _execute_local_tool(name: str, args: dict[str, Any], ticket_key: str) -> str:
    logger.info("[%s] -> Local tool: %s(%s)", ticket_key, name, list(args.keys()))
    if name == "list_files":
        result = _tool_list_files(args.get("subdir", ""))
    elif name == "read_file":
        result = _tool_read_file(args["path"])
    elif name == "write_files":
        result = _tool_write_files(args["files"], args.get("summary", ""), ticket_key)
    elif name == "create_git_branch":
        result = _tool_create_git_branch(args["branch_name"])
    elif name == "commit_and_push":
        result = _tool_commit_and_push(args["message"])
    else:
        result = json.dumps({"error": f"Unknown local tool: {name}"})
    logger.info("[%s] Local tool result: %s", ticket_key, result[:150].replace("\n", " "))
    return result


# ---------------------------------------------------------------------------
# Agentic loop (async - runs in FastAPI background task)
# ---------------------------------------------------------------------------
async def _run_agentic_loop(ticket_key: str) -> None:
    if not GITHUB_TOKEN:
        logger.warning("[%s] GITHUB_TOKEN not set - skipping. Set it in .env", ticket_key)
        return

    logger.info("[%s] Fetching Atlassian OAuth access token...", ticket_key)
    try:
        access_token = _get_atlassian_access_token()
    except Exception as exc:
        logger.error("[%s] Failed to get Atlassian access token: %s", ticket_key, exc)
        return

    mcp_headers: dict[str, str] = {"Authorization": f"Bearer {access_token}"}
    if ATLASSIAN_CLOUD_ID:
        mcp_headers["X-Atlassian-Cloud-Id"] = ATLASSIAN_CLOUD_ID

    async with streamablehttp_client(MCP_URL, headers=mcp_headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            list_result = await session.list_tools()
            mcp_tool_names = {t.name for t in list_result.tools}
            mcp_tools_openai = [_mcp_tool_to_openai(t) for t in list_result.tools]
            logger.info("[%s] Atlassian MCP tools loaded: %d tools", ticket_key, len(mcp_tool_names))
            logger.info("[%s] Tools: %s", ticket_key, sorted(mcp_tool_names))

            all_tools = LOCAL_TOOLS + mcp_tools_openai

            cloud_id = ATLASSIAN_CLOUD_ID
            system_prompt = (
                "You are an expert software engineer working autonomously on a software project.\n\n"
                "You have tools to read the codebase on demand — use them only when needed, not all at once.\n\n"
                "IMPORTANT: Never call search, getAccessibleAtlassianResources, or any discovery tools.\n"
                "You already know the Jira ticket key and cloud ID. Use them directly.\n\n"
                f"Jira ticket key : {ticket_key}\n"
                f"Atlassian cloudId: {cloud_id}\n\n"
                "Your workflow for every Jira ticket:\n"
                f"1. Call getJiraIssue with issueIdOrKey=\"{ticket_key}\" and cloudId=\"{cloud_id}\".\n"
                "2. Call list_files to see what exists in the workspace.\n"
                "3. Call read_file on only the specific files relevant to the ticket requirements.\n"
                "4. Implement all required changes by calling write_files with the FULL content of every file to create or modify.\n"
                f"5. Call create_git_branch with a branch name: feature/{ticket_key.lower()}-{{short-slug}}.\n"
                "6. Call commit_and_push with a clear commit message referencing the ticket key.\n"
                f"7. Call addCommentToJiraIssue with issueIdOrKey=\"{ticket_key}\", cloudId=\"{cloud_id}\", and a comment body containing:\n"
                "   - Summary of all changes implemented\n"
                "   - List of files created/modified\n"
                "   - The exact branch name\n"
                "   - Any important implementation notes\n"
            )

            client = AsyncOpenAI(base_url=GITHUB_MODELS_ENDPOINT, api_key=GITHUB_TOKEN)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Implement the changes required by Jira ticket {ticket_key}."},
            ]

            logger.info("[%s] Starting agentic loop - model: %s", ticket_key, MODEL_ID)

            for round_num in range(1, 15):
                logger.info("[%s] Round %d - calling model...", ticket_key, round_num)

                response = await client.chat.completions.create(
                    model=MODEL_ID,
                    messages=messages,
                    tools=all_tools,
                    tool_choice="auto",
                    temperature=0.1,
                )

                usage = response.usage
                if usage:
                    logger.info(
                        "[%s] Tokens - prompt: %d, completion: %d, total: %d",
                        ticket_key, usage.prompt_tokens, usage.completion_tokens, usage.total_tokens,
                    )

                choice = response.choices[0]
                messages.append(choice.message.model_dump(exclude_unset=True))

                if not choice.message.tool_calls:
                    logger.info("[%s] Agentic loop complete.", ticket_key)
                    if choice.message.content:
                        logger.info("[%s] Final: %s", ticket_key, choice.message.content[:300])
                    break

                for tc in choice.message.tool_calls:
                    args = json.loads(tc.function.arguments)
                    logger.info("[%s] -> Tool call: %s(%s)", ticket_key, tc.function.name, list(args.keys()))

                    if tc.function.name in mcp_tool_names:
                        result = await _call_mcp_tool(session, tc.function.name, args, ticket_key)
                    else:
                        result = _execute_local_tool(tc.function.name, args, ticket_key)

                    logger.info("[%s] Tool result: %s", ticket_key, result[:150].replace("\n", " "))
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            else:
                logger.warning("[%s] Reached max rounds (14) without finishing.", ticket_key)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run("jira_webhook:app", host="0.0.0.0", port=APP_PORT, reload=False)
