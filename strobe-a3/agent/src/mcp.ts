import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { SSMClient, GetParametersByPathCommand } from "@aws-sdk/client-ssm";

export const REGION = process.env.AWS_REGION ?? "ap-southeast-2";

const DEFAULTS: Record<string, string> = {
  "mcp-url": "http://127.0.0.1:8000/mcp",
  "chat-model-id": "google.gemma-3-12b-it",
  "max-steps": "8",
  "acp-port": "7331",
};

let ssmValues: Record<string, string> = {};

/** Load configuration from Parameter Store once at start-up (optional). */
export async function loadConfig(): Promise<void> {
  const prefix = process.env.AGENT_SSM_PREFIX;
  if (!prefix) return;
  try {
    const ssm = new SSMClient({ region: REGION });
    const out = await ssm.send(new GetParametersByPathCommand({ Path: prefix.replace(/\/$/, "") }));
    for (const p of out.Parameters ?? []) {
      const key = p.Name!.split("/").pop()!;
      ssmValues[key] = p.Value!;
    }
    console.log(`loaded ${Object.keys(ssmValues).length} parameters from ${prefix}`);
  } catch (error) {
    console.warn("Parameter Store unavailable; using defaults", error);
  }
}

export function cfg(key: string): string {
  const envKey = "AGENT_" + key.replace(/-/g, "_").toUpperCase();
  return process.env[envKey] ?? ssmValues[key] ?? DEFAULTS[key];
}

export interface McpTool {
  name: string;
  description: string;
  inputSchema: unknown;
}

/**
 * One MCP connection per ACP session, carrying that user's Cognito token.
 * The token travels on every HTTP request, so the MCP server can scope each
 * tool call to the caller without the agent ever passing a user id.
 */
export class StrobeMcp {
  private client: Client;
  private transport: StreamableHTTPClientTransport;
  tools: McpTool[] = [];

  constructor(private token: string) {
    this.client = new Client({ name: "strobe-acp-agent", version: "1.0.0" }, { capabilities: {} });
    this.transport = new StreamableHTTPClientTransport(new URL(cfg("mcp-url")), {
      requestInit: { headers: { Authorization: `Bearer ${this.token}` } },
    });
  }

  async connect(): Promise<void> {
    await this.client.connect(this.transport);
    const listed = await this.client.listTools();
    this.tools = listed.tools.map((t) => ({
      name: t.name,
      description: t.description ?? "",
      inputSchema: t.inputSchema,
    }));
    console.log(`MCP connected: ${this.tools.map((t) => t.name).join(", ")}`);
  }

  async call(name: string, args: Record<string, unknown>): Promise<string> {
    const result = await this.client.callTool({ name, arguments: args });
    const blocks = (result.content ?? []) as Array<{ type: string; text?: string }>;
    const text = blocks.filter((b) => b.type === "text").map((b) => b.text ?? "").join("\n");
    // A tool that reports an error must not be handed to the model as success.
    if (result.isError) throw new Error(text || "tool returned an error");
    return text || JSON.stringify(result);
  }

  async close(): Promise<void> {
    try {
      await this.client.close();
    } catch {
      // the session is going away anyway
    }
  }
}
