import * as acp from "@agentclientprotocol/sdk";
import { createWebSocketStream } from "@agentclientprotocol/sdk/experimental/ws-client";
import WebSocket from "ws";
globalThis.WebSocket = WebSocket;

const updates = [];
const client = acp.client({ name: "probe" })
  .onNotification(acp.methods.client.session.update, (ctx) => updates.push(ctx.params.update));
const conn = client.connect(createWebSocketStream(process.env.ACP_URL ?? "ws://127.0.0.1:7331/acp"));
await conn.agent.request(acp.methods.agent.initialize, { protocolVersion: acp.PROTOCOL_VERSION, clientCapabilities: {} });
const s = await conn.agent.request(acp.methods.agent.session.new, { cwd: "/", mcpServers: [], _meta: { strobeToken: process.env.AGENT_DEV_TOKEN ?? "" } });
const res = await conn.agent.request(acp.methods.agent.session.prompt, {
  sessionId: s.sessionId, prompt: [{ type: "text", text: process.argv.slice(2).join(" ") || "find my dog photos" }],
});
console.log("stopReason:", res.stopReason);
for (const u of updates) console.log(u.sessionUpdate, JSON.stringify(u.content ?? u.title ?? u.status ?? "").slice(0, 110));
conn.close();
process.exit(0);
