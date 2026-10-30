import {
  BedrockRuntimeClient,
  ConverseCommand,
  type ContentBlock,
  type Message,
} from "@aws-sdk/client-bedrock-runtime";
import { cfg, REGION, type McpTool } from "./mcp.js";

const brt = new BedrockRuntimeClient({ region: REGION });

/**
 * Gemma 3 accepts Bedrock's toolConfig but ignores it (verified against the
 * course models: it answered from memory and invented a date instead of
 * calling the tool). So the agent runs its own tool protocol: the model is
 * asked for a single JSON object, which we parse and dispatch ourselves.
 */
export function systemPrompt(tools: McpTool[], today: string): string {
  const list = tools
    .map((t) => `- ${t.name}: ${t.description.split("\n")[0]}\n  arguments: ${JSON.stringify(t.inputSchema)}`)
    .join("\n");
  return `You are Strobe Assistant. You help one person explore their own photos and posts on Strobe.
Today is ${today}.

You cannot see their content directly. To find anything you MUST call a tool.
Never invent photos, captions, dates or events: if a tool did not return it, say you could not find it.

Available tools:
${list}

Reply with ONE JSON object and nothing else. No prose outside the JSON, no code fences.

To call a tool:
{"thought": "<one short line of reasoning>", "tool": "<tool name>", "arguments": {<arguments>}}

To answer the user:
{"thought": "<one short line>", "tool": null, "answer": "<your reply in markdown>"}

Rules:
- Search before answering any question about their content, even if it seems obvious.
- Use search_my_content for "find/show me ..." questions; the query should describe the subject.
- To show photos, call get_image_urls with the image ids you found, then reference each photo in
  your answer as markdown using its image id, exactly like this: ![caption](IMAGE:img101)
  Never write a real https url yourself; the id form is replaced with a working link for you.
- When narrating an album, first call list_my_posts to see how this person writes, then write the
  story in their voice: same tone and length of sentence, no flowery language they would not use.
- Search results are ranked by distance: lower is closer. Only show the ones that actually
  answer the question. If the user asked for dogs, do not list their beach and food photos
  just because the tool returned them.
- If nothing matches well, say so rather than offering the closest unrelated thing.
- Keep answers short and concrete.`;
}

export interface ModelStep {
  thought?: string;
  tool: string | null;
  arguments?: Record<string, unknown>;
  answer?: string;
}

/** Pull the first JSON object out of a reply, tolerating fences and stray prose. */
export function parseStep(text: string): ModelStep | null {
  const fenced = text.replace(/```(?:json)?/gi, " ");
  const start = fenced.indexOf("{");
  if (start === -1) return null;
  // Walk to the matching brace so trailing commentary does not break the parse.
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = start; i < fenced.length; i++) {
    const ch = fenced[i];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (ch === "\\") {
      escaped = true;
      continue;
    }
    if (ch === '"') inString = !inString;
    if (inString) continue;
    if (ch === "{") depth++;
    if (ch === "}") {
      depth--;
      if (depth === 0) {
        try {
          const parsed = JSON.parse(fenced.slice(start, i + 1));
          if (typeof parsed !== "object" || parsed === null) return null;
          return {
            thought: typeof parsed.thought === "string" ? parsed.thought : undefined,
            tool: typeof parsed.tool === "string" ? parsed.tool : null,
            arguments: typeof parsed.arguments === "object" && parsed.arguments ? parsed.arguments : {},
            answer: typeof parsed.answer === "string" ? parsed.answer : undefined,
          };
        } catch {
          return null;
        }
      }
    }
  }
  return null;
}

export async function converse(system: string, messages: Message[]): Promise<string> {
  if (process.env.BEDROCK_STUB === "1") return stubReply(messages);
  const response = await brt.send(
    new ConverseCommand({
      modelId: cfg("chat-model-id"),
      system: [{ text: system }],
      messages,
      inferenceConfig: { maxTokens: 1024, temperature: 0 },
    }),
  );
  const blocks = (response.output?.message?.content ?? []) as ContentBlock[];
  return blocks.map((b) => ("text" in b ? b.text ?? "" : "")).join("");
}

/** Offline replies so the ACP plumbing can be tested without Bedrock credentials. */
function stubReply(messages: Message[]): string {
  const last = JSON.stringify(messages[messages.length - 1] ?? {});
  if (last.includes("TOOL RESULT")) {
    return JSON.stringify({ thought: "got results", tool: null, answer: "Stubbed answer from tool results." });
  }
  return JSON.stringify({
    thought: "need to search",
    tool: "search_my_content",
    arguments: { query: "dogs", top_k: 3 },
  });
}
