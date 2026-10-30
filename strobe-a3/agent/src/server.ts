/**
 * Strobe ACP agent.
 *
 * Speaks the Agent Client Protocol to the browser client over WebSocket, and
 * MCP to the Strobe MCP server for data. The model never touches AWS directly:
 * every fact in an answer arrives through an MCP tool result.
 *
 * The web client has no auth of its own, and browsers cannot set WebSocket
 * headers, so the user's Cognito token arrives in `_meta` on session/new and is
 * held for the life of that session.
 */
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { WebSocketServer } from "ws";
import * as acp from "@agentclientprotocol/sdk";
import { createNodeHttpHandler, createNodeWebSocketUpgradeHandler } from "@agentclientprotocol/sdk/experimental/node";
import { AcpServer } from "@agentclientprotocol/sdk/experimental/server";
import type { Message } from "@aws-sdk/client-bedrock-runtime";

import { cfg, loadConfig, StrobeMcp } from "./mcp.js";
import { converse, parseStep, systemPrompt } from "./model.js";

interface Session {
  mcp: StrobeMcp | null;
  history: Message[];
  pending: AbortController | null;
}

const sessions = new Map<string, Session>();

function textOf(prompt: readonly acp.ContentBlock[]): string {
  return prompt.flatMap((p) => (p.type === "text" ? [p.text] : [])).join("\n").trim();
}

function imageOf(prompt: readonly acp.ContentBlock[]): { data: string; mimeType: string } | null {
  const part = prompt.find((p) => p.type === "image");
  return part && part.type === "image" ? { data: part.data, mimeType: part.mimeType } : null;
}

class StrobeAgent {
  initialize(): acp.InitializeResponse {
    return { protocolVersion: acp.PROTOCOL_VERSION, agentCapabilities: { loadSession: false } };
  }

  async newSession(params: acp.NewSessionRequest): Promise<acp.NewSessionResponse> {
    const sessionId = crypto.randomUUID();
    // The client puts the Cognito token here after the user signs in.
    const meta = (params as unknown as { _meta?: Record<string, unknown> })._meta ?? {};
    const token = typeof meta.strobeToken === "string" ? meta.strobeToken : process.env.AGENT_DEV_TOKEN ?? "";

    let mcp: StrobeMcp | null = null;
    if (token) {
      mcp = new StrobeMcp(token);
      try {
        await mcp.connect();
      } catch (error) {
        console.error("MCP connect failed", error);
        mcp = null;
      }
    } else {
      console.warn("session created without a token: tools will be unavailable");
    }

    sessions.set(sessionId, { mcp, history: [], pending: null });
    return { sessionId };
  }

  async prompt(params: acp.PromptRequest, client: acp.AgentContext): Promise<acp.PromptResponse> {
    const session = sessions.get(params.sessionId);
    if (!session) throw new Error(`Unknown session: ${params.sessionId}`);

    session.pending?.abort();
    const controller = new AbortController();
    session.pending = controller;

    const notify = (update: Record<string, unknown>) =>
      client.notify(acp.methods.client.session.update, { sessionId: params.sessionId, update });
    const say = (text: string) =>
      notify({ sessionUpdate: "agent_message_chunk", content: { type: "text", text } });

    try {
      if (!session.mcp) {
        await say("I can't reach your Strobe content: please sign in again.");
        return { stopReason: "end_turn" };
      }

      const userText = textOf(params.prompt);
      const image = imageOf(params.prompt);
      if (image) {
        // An attached photo becomes a similarity search rather than a chat image.
        session.history.push({
          role: "user",
          content: [{
            text: `${userText || "Find photos like this one."}\n\n(The user attached an image. ` +
                  `Call find_similar_images with image_base64 set to "ATTACHED".)`,
          }],
        });
      } else {
        session.history.push({ role: "user", content: [{ text: userText }] });
      }

      const system = systemPrompt(session.mcp.tools, new Date().toISOString().slice(0, 10));
      // Presigned urls are ~1 kB of escaped query string: far too long for the model to copy
      // into JSON reliably. It writes ![caption](IMAGE:<imageId>) and we substitute here.
      const imageUrls = new Map<string, string>();
      const IMAGE_REF = /IMAGE:([A-Za-z0-9._-]+)/g;

      // Resolve ![caption](IMAGE:<id>) placeholders. If the model referenced a
      // photo without calling get_image_urls first, fetch the links here rather
      // than spending another model step asking it to.
      const resolveImages = async (text: string): Promise<string> => {
        const ids = [...text.matchAll(IMAGE_REF)].map((m) => m[1]);
        const missing = [...new Set(ids)].filter((id) => !imageUrls.has(id));
        if (missing.length && session.mcp) {
          try {
            const result = await session.mcp.call("get_image_urls", { image_ids: missing });
            for (const match of result.matchAll(/"imageId":\s*"([^"]+)"[\s\S]*?"url":\s*"([^"]+)"/g)) {
              imageUrls.set(match[1], match[2]);
            }
          } catch (error) {
            console.error("could not resolve image links", error);
          }
        }
        return text.replace(IMAGE_REF, (whole, id) => imageUrls.get(id) ?? whole);
      };
      const maxSteps = Number(cfg("max-steps"));
      let toolsUsed = 0;

      for (let step = 0; step < maxSteps; step++) {
        if (controller.signal.aborted) return { stopReason: "cancelled" };

        const raw = await converse(system, session.history);
        const parsed = parseStep(raw);

        if (!parsed) {
          // Small models often drop the JSON wrapper once they have what they need.
          // If tools have already run, treat plain prose as the answer instead of
          // burning steps on corrections.
          if (toolsUsed > 0 && raw.trim().length > 0) {
            await say(await resolveImages(raw.trim()));
            await say(`\n\n_Answered from ${toolsUsed} tool call${toolsUsed === 1 ? "" : "s"} against your Strobe library._`);
            return { stopReason: "end_turn" };
          }
          session.history.push({ role: "assistant", content: [{ text: raw }] });
          session.history.push({
            role: "user",
            content: [{ text: "That was not valid JSON. Reply with only the JSON object." }],
          });
          continue;
        }

        session.history.push({ role: "assistant", content: [{ text: raw }] });

        if (parsed.thought) {
          await notify({ sessionUpdate: "agent_thought_chunk", content: { type: "text", text: parsed.thought } });
        }

        if (!parsed.tool) {
          const answer = parsed.answer ?? "I could not work that out.";
          await say(await resolveImages(answer));
          if (toolsUsed > 0) {
            await say(`\n\n_Answered from ${toolsUsed} tool call${toolsUsed === 1 ? "" : "s"} against your Strobe library._`);
          }
          return { stopReason: "end_turn" };
        }

        const args = { ...(parsed.arguments ?? {}) };
        if (args.image_base64 === "ATTACHED" && image) args.image_base64 = image.data;

        const toolCallId = `call-${step}`;
        await notify({
          sessionUpdate: "tool_call",
          toolCallId,
          title: parsed.tool,
          kind: "search",
          status: "in_progress",
          rawInput: args.image_base64 ? { ...args, image_base64: "<attached image>" } : args,
        });

        let result: string;
        try {
          result = await session.mcp.call(parsed.tool, args);
          toolsUsed++;
          await notify({ sessionUpdate: "tool_call_update", toolCallId, status: "completed" });
        } catch (error) {
          result = `ERROR: ${(error as Error).message}`;
          await notify({ sessionUpdate: "tool_call_update", toolCallId, status: "failed" });
        }

        let visible = result;
        if (parsed.tool === "get_image_urls") {
          // Remember the urls, show the model only the ids.
          const ids: string[] = [];
          for (const match of result.matchAll(/"imageId":\s*"([^"]+)"[\s\S]*?"url":\s*"([^"]+)"/g)) {
            imageUrls.set(match[1], match[2]);
            ids.push(match[1]);
          }
          visible = ids.length
            ? `Links ready for: ${ids.join(", ")}. Reference them as ![caption](IMAGE:<id>).`
            : "No viewable images found.";
        }
        session.history.push({
          role: "user",
          content: [{ text: `TOOL RESULT for ${parsed.tool}:\n${visible.slice(0, 6000)}` }],
        });
      }

      await say("I ran out of steps before finding an answer. Could you narrow that down?");
      return { stopReason: "end_turn" };
    } catch (error) {
      if (controller.signal.aborted) return { stopReason: "cancelled" };
      console.error("prompt failed", error);
      await say(`Something went wrong: ${(error as Error).message}`);
      return { stopReason: "end_turn" };
    } finally {
      if (session.pending === controller) session.pending = null;
    }
  }

  cancel(params: acp.CancelNotification): void {
    sessions.get(params.sessionId)?.pending?.abort();
  }
}

function pathName(request: IncomingMessage): string {
  return new URL(request.url ?? "/", `http://${request.headers.host ?? "localhost"}`).pathname;
}

function plain(response: ServerResponse, status: number, body: string): void {
  const payload = Buffer.from(body, "utf8");
  response.writeHead(status, { "Content-Type": "text/plain; charset=utf-8", "Content-Length": payload.byteLength });
  response.end(payload);
}

async function main(): Promise<void> {
  await loadConfig();
  const implementation = new StrobeAgent();
  const agent = acp
    .agent({ name: "strobe-acp-agent" })
    .onRequest(acp.methods.agent.initialize, () => implementation.initialize())
    .onRequest(acp.methods.agent.session.new, (ctx) => implementation.newSession(ctx.params))
    .onRequest(acp.methods.agent.session.prompt, (ctx) => implementation.prompt(ctx.params, ctx.client))
    .onNotification(acp.methods.agent.session.cancel, (ctx) => implementation.cancel(ctx.params));

  const acpServer = new AcpServer({ agent });
  const httpHandler = createNodeHttpHandler(acpServer);
  const wss = new WebSocketServer({ noServer: true });
  const upgradeHandler = createNodeWebSocketUpgradeHandler(acpServer, wss);

  const port = Number(cfg("acp-port"));
  const server = createServer((request, response) => {
    const path = pathName(request);
    if (path === "/healthz") plain(response, 200, "ok\n");
    else if (path === "/acp") httpHandler(request, response);
    else plain(response, 404, "Not found\n");
  });

  server.on("upgrade", (request, socket, head) => {
    if (pathName(request) !== "/acp") {
      socket.destroy();
      return;
    }
    upgradeHandler(request, socket, head);
  });

  server.listen(port, "0.0.0.0", () => {
    console.log(`Strobe ACP agent: ws://0.0.0.0:${port}/acp  (MCP at ${cfg("mcp-url")})`);
  });

  const shutdown = () => {
    for (const session of sessions.values()) void session.mcp?.close();
    server.close();
  };
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
}

void main();
