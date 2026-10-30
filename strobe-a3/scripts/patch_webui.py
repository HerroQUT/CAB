"""Add Cognito sign-in to the provided ACP web client.

The client ships with no authentication and a hardcoded WebSocket endpoint.
Three changes, applied in place:

  1. index.html   - a sign-in panel, and a <script> for the generated config.js
  2. web/main.ts  - read the endpoint from config.js, sign in against the Strobe
                    API, and pass the Cognito token in `_meta` on session/new
  3. Dockerfile   - bake the endpoint and API URL in as build arguments

Browsers cannot set headers on a WebSocket, which is why the token travels in
`_meta` rather than an Authorization header.

    python scripts/patch_webui.py --webui-dir ~/path/to/acp_client

Safe to re-run: each change is skipped if already present.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

LOGIN_HTML = """      <section id="signin" class="composer" aria-label="Sign in">
        <h2 style="margin:0 0 .5rem;font-size:1.05rem">Sign in to Strobe</h2>
        <p class="hint" style="margin:0 0 .75rem">Your photos stay yours: the assistant only ever sees the content of the account you sign in with.</p>
        <div style="display:flex;gap:.5rem;flex-wrap:wrap">
          <input id="signin-email" type="email" placeholder="email" autocomplete="username"
                 style="flex:1 1 14rem;border:1px solid #d1d5db;border-radius:.65rem;padding:.7rem .8rem" />
          <input id="signin-password" type="password" placeholder="password" autocomplete="current-password"
                 style="flex:1 1 10rem;border:1px solid #d1d5db;border-radius:.65rem;padding:.7rem .8rem" />
          <button id="signin-button" type="button">Sign in</button>
        </div>
        <p id="signin-error" class="hint" style="color:#b91c1c"></p>
      </section>

"""

MAIN_TS_CONFIG = '''declare global {
  interface Window {
    STROBE_CONFIG?: { acpEndpoint?: string; strobeApi?: string };
  }
}

// Set by /config.js, which the container generates from build arguments.
const ACP_WEBSOCKET_ENDPOINT = window.STROBE_CONFIG?.acpEndpoint ?? "ws://127.0.0.1:7331/acp";
const STROBE_API = window.STROBE_CONFIG?.strobeApi ?? "http://127.0.0.1:3000";
let strobeToken = "";
'''

LOGIN_TS = '''
// --- Cognito sign-in -------------------------------------------------------
// The Strobe API returns a Cognito token; the agent forwards it to the MCP
// server, which verifies it and scopes every tool call to that user.
const signinPanel = getElement<HTMLElement>("signin");
const signinEmail = getElement<HTMLInputElement>("signin-email");
const signinPassword = getElement<HTMLInputElement>("signin-password");
const signinButton = getElement<HTMLButtonElement>("signin-button");
const signinError = getElement<HTMLElement>("signin-error");

async function signIn(): Promise<void> {
  const email = signinEmail.value.trim();
  const password = signinPassword.value;
  if (!email || !password) {
    signinError.textContent = "Enter your email and password.";
    return;
  }
  signinButton.disabled = true;
  signinError.textContent = "";
  setStatus("Signing in", "working");
  try {
    const response = await fetch(`${STROBE_API}/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!response.ok) throw new Error(`Sign-in failed (${response.status})`);
    const data = await response.json();
    const token = data.token ?? data.accessToken ?? data.idToken ?? data.tokens?.accessToken;
    if (typeof token !== "string") throw new Error("No token in the sign-in response.");
    strobeToken = token;
    signinPanel.hidden = true;
    signinPassword.value = "";
    await connect();
  } catch (error) {
    signinError.textContent = errorMessage(error);
    setStatus("Sign-in failed", "error");
  } finally {
    signinButton.disabled = false;
  }
}

signinButton.addEventListener("click", () => void signIn());
signinPassword.addEventListener("keydown", (event) => {
  if (event.key === "Enter") void signIn();
});
'''

DOCKERFILE = """FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:1.29-alpine
# Where the agent lives, and where the client signs in. Both are baked in at
# build time by scripts/deploy_services.py.
ARG ACP_ENDPOINT="ws://127.0.0.1:7331/acp"
ARG STROBE_API="http://127.0.0.1:3000"
COPY --from=build /app/dist /usr/share/nginx/html
RUN printf 'window.STROBE_CONFIG=%s;\\n' \\
    "{\\"acpEndpoint\\":\\"${ACP_ENDPOINT}\\",\\"strobeApi\\":\\"${STROBE_API}\\"}" \\
    > /usr/share/nginx/html/config.js
EXPOSE 80
"""


def patch_index(path: str) -> None:
    s = open(path).read()
    if 'id="signin"' in s:
        print("  index.html already patched")
        return
    s = s.replace('<link rel="stylesheet" href="/web/styles.css" />',
                  '<link rel="stylesheet" href="/web/styles.css" />\n    <script src="/config.js"></script>')
    marker = '      <section id="messages"'
    if marker not in s:
        sys.exit("index.html: could not find the messages section")
    s = s.replace(marker, LOGIN_HTML + marker, 1)
    open(path, "w").write(s)
    print("  patched index.html")


def patch_main(path: str) -> None:
    s = open(path).read()
    if "STROBE_CONFIG" in s:
        print("  main.ts already patched")
        return

    old_const = ('// -----------------------------------------------------------------------------\n'
                 '// STUDENT CONFIGURATION: set this to the ACP agent\'s WebSocket endpoint.\n'
                 '// Use wss:// when the web client is served over HTTPS.\n'
                 '// -----------------------------------------------------------------------------\n'
                 'const ACP_WEBSOCKET_ENDPOINT = "ws://127.0.0.1:7331/acp";')
    if old_const in s:
        s = s.replace(old_const, MAIN_TS_CONFIG)
    elif 'const ACP_WEBSOCKET_ENDPOINT = "ws://127.0.0.1:7331/acp";' in s:
        s = s.replace('const ACP_WEBSOCKET_ENDPOINT = "ws://127.0.0.1:7331/acp";', MAIN_TS_CONFIG)
    else:
        sys.exit("main.ts: could not find ACP_WEBSOCKET_ENDPOINT")

    # Carry the token to the agent on session creation.
    old_session = '''    const session = await nextConnection.agent.request(acp.methods.agent.session.new, {
      cwd: "/",
      mcpServers: [],
    });'''
    new_session = '''    const session = await nextConnection.agent.request(acp.methods.agent.session.new, {
      cwd: "/",
      mcpServers: [],
      // Browsers cannot set WebSocket headers, so the Cognito token rides here.
      _meta: { strobeToken },
    });'''
    if old_session not in s:
        sys.exit("main.ts: could not find the session/new call")
    s = s.replace(old_session, new_session)

    # Wait for sign-in instead of connecting on load.
    old_tail = "applyTheme();\nvoid connect().catch(reportConnectionError);"
    if old_tail not in s:
        sys.exit("main.ts: could not find the startup call")
    s = s.replace(old_tail, LOGIN_TS + '\napplyTheme();\nsetStatus("Sign in to start", "idle");\nsigninEmail.focus();')

    open(path, "w").write(s)
    print("  patched web/main.ts")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--webui-dir", required=True)
    args = ap.parse_args()
    root = os.path.expanduser(args.webui_dir)

    index = os.path.join(root, "index.html")
    main_ts = os.path.join(root, "web", "main.ts")
    dockerfile = os.path.join(root, "Dockerfile")
    for path in (index, main_ts, dockerfile):
        if not os.path.isfile(path):
            sys.exit(f"not found: {path}\nIs --webui-dir the unzipped ACP client?")

    for path in (index, main_ts, dockerfile):
        backup = path + ".orig"
        if not os.path.exists(backup):
            shutil.copy2(path, backup)
    print("Backups written alongside each file (.orig)")

    patch_index(index)
    patch_main(main_ts)
    open(dockerfile, "w").write(DOCKERFILE)
    print("  rewrote Dockerfile (ACP_ENDPOINT / STROBE_API build args)")

    print("\nCheck it compiles:")
    print(f"  cd {root} && npm install && npm run build")


if __name__ == "__main__":
    main()
