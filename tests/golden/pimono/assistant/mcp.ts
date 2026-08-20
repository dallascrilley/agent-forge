/**
 * Register MCP tools from mcp.json. Do not copy mcp.json into ~/.pi.
 */
import { spawn, spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

function requireTool(name: string) {
  const r = spawnSync("python3", ["guardrails.py", "check-tool", name], {
    cwd: process.cwd(),
    encoding: "utf8",
  });
  if (r.status !== 0) {
    throw new Error((r.stderr || r.stdout || "tool refused").trim());
  }
}

type ServerCfg = {
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  headers?: Record<string, string>;
};

type RpcReply = { id?: number; result?: unknown; error?: { message?: string } };

function loadServers(): Record<string, ServerCfg> {
  const raw = JSON.parse(readFileSync(join(process.cwd(), "mcp.json"), "utf8"));
  return (raw.mcpServers || {}) as Record<string, ServerCfg>;
}

function encode(msg: unknown): string {
  return JSON.stringify(msg) + "\n";
}

class StdioClient {
  proc: ReturnType<typeof spawn>;
  pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void }>();
  nextId = 1;
  buf = "";

  constructor(cfg: ServerCfg, cwd: string) {
    this.proc = spawn(cfg.command!, cfg.args || [], {
      cwd,
      env: { ...process.env, ...(cfg.env || {}) },
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.proc.stderr?.on("data", () => {});
    this.proc.stdout?.setEncoding("utf8");
    this.proc.stdout?.on("data", (chunk: string) => this.onData(chunk));
    this.proc.on("exit", () => {
      for (const p of this.pending.values()) p.reject(new Error("mcp server exited"));
      this.pending.clear();
    });
  }

  onData(chunk: string) {
    this.buf += chunk;
    while (this.buf.length) {
      if (/^Content-Length:/i.test(this.buf)) {
        const m = this.buf.match(/^Content-Length:\s*(\d+)/i);
        const idx = this.buf.indexOf("\r\n\r\n");
        if (!m || idx < 0) return;
        const n = Number(m[1]);
        const start = idx + 4;
        if (this.buf.length < start + n) return;
        this.dispatch(JSON.parse(this.buf.slice(start, start + n)));
        this.buf = this.buf.slice(start + n);
        continue;
      }
      const nl = this.buf.indexOf("\n");
      if (nl < 0) return;
      const line = this.buf.slice(0, nl).trim();
      this.buf = this.buf.slice(nl + 1);
      if (line) this.dispatch(JSON.parse(line));
    }
  }

  dispatch(msg: RpcReply) {
    if (msg.id == null) return;
    const p = this.pending.get(msg.id);
    if (!p) return;
    this.pending.delete(msg.id);
    if (msg.error) p.reject(new Error(msg.error.message || "mcp error"));
    else p.resolve(msg.result);
  }

  call(method: string, params?: unknown): Promise<unknown> {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("mcp timeout: " + method));
      }, 30000);
      this.pending.set(id, {
        resolve: (v) => {
          clearTimeout(t);
          resolve(v);
        },
        reject: (e) => {
          clearTimeout(t);
          reject(e);
        },
      });
      this.proc.stdin?.write(encode({ jsonrpc: "2.0", id, method, params }));
    });
  }

  notify(method: string, params?: unknown) {
    this.proc.stdin?.write(encode({ jsonrpc: "2.0", method, params }));
  }

  close() {
    this.proc.kill();
  }
}

class HttpClient {
  url: string;
  headers: Record<string, string>;
  sessionId: string | undefined;
  nextId = 1;

  constructor(cfg: ServerCfg) {
    this.url = cfg.url!;
    this.headers = { ...(cfg.headers || {}) };
  }

  async post(payload: unknown): Promise<RpcReply> {
    const res = await fetch(this.url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/json, text/event-stream",
        ...(this.sessionId ? { "mcp-session-id": this.sessionId } : {}),
        ...this.headers,
      },
      body: JSON.stringify(payload),
    });
    const sid = res.headers.get("mcp-session-id");
    if (sid) this.sessionId = sid;
    const text = await res.text();
    if (!res.ok) {
      throw new Error("mcp http " + res.status + " " + text.slice(0, 200));
    }
    const ctype = (res.headers.get("content-type") || "").toLowerCase();
    const sse =
      ctype.includes("text/event-stream") || /^\s*(event:|data:)/m.test(text);
    if (sse) {
      const data = text
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trim())
        .filter(Boolean)
        .join("\n");
      if (!data) throw new Error("mcp empty sse");
      return JSON.parse(data) as RpcReply;
    }
    return text ? (JSON.parse(text) as RpcReply) : {};
  }

  async call(method: string, params?: unknown): Promise<unknown> {
    const parsed = await this.post({
      jsonrpc: "2.0",
      id: this.nextId++,
      method,
      params,
    });
    if (parsed.error) throw new Error(parsed.error.message || "mcp error");
    return parsed.result;
  }

  async notify(method: string, params?: unknown) {
    await this.post({ jsonrpc: "2.0", method, params });
  }

  close() {}
}

type Client = StdioClient | HttpClient;

async function handshake(client: Client) {
  await client.call("initialize", {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "agent-forge", version: "1" },
  });
  await Promise.resolve(client.notify("notifications/initialized"));
}

export default async function (pi: ExtensionAPI) {
  const cwd = process.cwd();
  const clients: Client[] = [];
  pi.on("session_shutdown", () => {
    for (const c of clients) c.close();
  });
  for (const [name, cfg] of Object.entries(loadServers())) {
    const client: Client = cfg.command
      ? new StdioClient(cfg, cwd)
      : new HttpClient(cfg);
    clients.push(client);
    try {
      await handshake(client);
      let cursor: string | undefined;
      do {
        const listed = (await client.call(
          "tools/list",
          cursor ? { cursor } : {},
        )) as { tools?: { name: string; description?: string; inputSchema?: object }[]; nextCursor?: string };
        for (const tool of listed.tools || []) {
          const schema = tool.inputSchema ?? { type: "object", properties: {} };
          pi.registerTool({
            name: tool.name,
            label: tool.name,
            description: tool.description || name + " MCP tool",
            parameters: schema as never,
            async execute(_id, params) {
              requireTool(tool.name);
              const result = (await client.call("tools/call", {
                name: tool.name,
                arguments: params,
              })) as {
                content?: { type?: string; text?: string }[];
                isError?: boolean;
              };
              const text = (result.content || [])
                .map((part) =>
                  part.type === "text" ? part.text : JSON.stringify(part),
                )
                .join("\n");
              return {
                content: [
                  {
                    type: "text" as const,
                    text: text || (result.isError ? "error" : "ok"),
                  },
                ],
                details: { server: name, isError: !!result.isError },
                isError: !!result.isError,
              };
            },
          });
        }
        cursor = listed.nextCursor;
      } while (cursor);
    } catch (err) {
      client.close();
      const i = clients.indexOf(client);
      if (i >= 0) clients.splice(i, 1);
      throw err;
    }
  }
}
