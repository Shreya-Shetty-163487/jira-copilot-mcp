"""
Run this script ONCE to complete the Atlassian OAuth 2.0 (3LO) flow.
It will:
  1. Open a browser where you approve access
  2. Catch the callback on localhost:8080
  3. Exchange the code for tokens
  4. Append ATLASSIAN_REFRESH_TOKEN and ATLASSIAN_CLOUD_ID to your .env

Usage:
    python atlassian_auth.py
"""

import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from dotenv import load_dotenv, set_key

load_dotenv()

CLIENT_ID = os.getenv("ATLASSIAN_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("ATLASSIAN_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("ATLASSIAN_REDIRECT_URI", "http://localhost:8080/callback")
ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")

SCOPES = " ".join([
    "read:jira-work",
    "write:jira-work",
    "read:jira-user",
    "manage:jira-project",
    "read:me",
    "offline_access",   # required to get a refresh_token
])

AUTH_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

auth_code: str = ""
server_done = threading.Event()


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
                <html><body style='font-family:sans-serif;padding:2rem'>
                <h2>Authorization successful!</h2>
                <p>You can close this tab and return to the terminal.</p>
                </body></html>
            """)
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing authorization code.")
        server_done.set()

    def log_message(self, format, *args):
        pass  # suppress HTTP server logs


def run_callback_server():
    port = int(REDIRECT_URI.split(":")[-1].split("/")[0])
    server = HTTPServer(("localhost", port), CallbackHandler)
    server.handle_request()


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print(
            "ERROR: ATLASSIAN_CLIENT_ID and ATLASSIAN_CLIENT_SECRET must be set in .env\n"
            "Follow Step 1-4 in the instructions first."
        )
        return

    # Step 1 — build the authorization URL
    params = {
        "audience": "api.atlassian.com",
        "client_id": CLIENT_ID,
        "scope": SCOPES,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "prompt": "consent",
    }
    url = f"{AUTH_URL}?{urlencode(params)}"

    # Step 2 — start local callback server in background thread
    t = threading.Thread(target=run_callback_server, daemon=True)
    t.start()

    # Step 3 — open browser
    print(f"\nOpening browser for Atlassian authorization...\n{url}\n")
    webbrowser.open(url)

    # Step 4 — wait for callback
    server_done.wait(timeout=120)
    if not auth_code:
        print("ERROR: Timed out waiting for authorization. Try again.")
        return

    print(f"Authorization code received.")

    # Step 5 — exchange code for tokens
    resp = requests.post(TOKEN_URL, json={
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": auth_code,
        "redirect_uri": REDIRECT_URI,
    })
    resp.raise_for_status()
    tokens = resp.json()

    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token", "")

    if not refresh_token:
        print("WARNING: No refresh_token returned. Make sure 'offline_access' scope is added.")

    # Step 6 — fetch cloud ID (needed to call api.atlassian.com)
    resources_resp = requests.get(
        ACCESSIBLE_RESOURCES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resources_resp.raise_for_status()
    resources = resources_resp.json()

    if not resources:
        print("ERROR: No Atlassian sites found for this account.")
        return

    print("\nAvailable Atlassian sites:")
    for i, r in enumerate(resources):
        print(f"  [{i}] {r['name']} — {r['url']}  (id: {r['id']})")

    choice = 0
    if len(resources) > 1:
        choice = int(input("\nEnter the number of the site to use: "))

    cloud_id = resources[choice]["id"]
    cloud_url = resources[choice]["url"]

    # Step 7 — save to .env
    set_key(ENV_FILE, "ATLASSIAN_REFRESH_TOKEN", refresh_token)
    set_key(ENV_FILE, "ATLASSIAN_CLOUD_ID", cloud_id)
    set_key(ENV_FILE, "JIRA_BASE_URL", cloud_url)

    print(f"\nSuccess! Saved to .env:")
    print(f"  ATLASSIAN_REFRESH_TOKEN = {refresh_token[:12]}...")
    print(f"  ATLASSIAN_CLOUD_ID      = {cloud_id}")
    print(f"  JIRA_BASE_URL           = {cloud_url}")
    print("\nYou can now run: python jira_webhook.py")


if __name__ == "__main__":
    main()
