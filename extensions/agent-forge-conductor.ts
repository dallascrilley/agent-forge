import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { StringEnum } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type, type Static } from "typebox";

const MAX_OUTPUT_BYTES = 50 * 1024;
const ACTIONS = ["catalog", "preview", "spawn", "status", "collect", "cancel", "integrate"] as const;
type Action = (typeof ACTIONS)[number];
type CoreAction = Action | "digest";

const DelegateParameters = Type.Object(
  {
    action: StringEnum(ACTIONS, { description: "The typed orchestration operation to perform." }),
    request: Type.Optional(Type.Any({ description: "Constrained WorkerRequest intent for preview or spawn." })),
    runId: Type.Optional(Type.String({ description: "Durable run identifier." })),
    workerId: Type.Optional(Type.String({ description: "Worker identifier for collect or integrate." })),
    reason: Type.Optional(Type.String({ description: "Cancellation reason." })),
  },
  { additionalProperties: false },
);

type DelegateParams = Static<typeof DelegateParameters>;

type CoreInput = {
  action: CoreAction;
  request?: unknown;
  runId?: string;
  workerId?: string;
  reason?: string;
  repositoryRoot?: string;
  repositoryId?: string;
  backend?: string;
};

const ACTION_FIELDS: Record<Action, ReadonlySet<string>> = {
  catalog: new Set(),
  preview: new Set(["request", "runId", "workerId"]),
  spawn: new Set(["request", "runId", "workerId"]),
  status: new Set(["runId"]),
  collect: new Set(["runId", "workerId"]),
  cancel: new Set(["runId", "reason"]),
  integrate: new Set(["runId", "workerId"]),
};

function assertActionShape(params: DelegateParams): void {
  const allowed = ACTION_FIELDS[params.action];
  for (const field of Object.keys(params)) {
    if (field !== "action" && !allowed.has(field)) {
      throw new Error(`${params.action} does not accept field ${field}`);
    }
  }

  if ((params.action === "preview" || params.action === "spawn") && (!params.request || typeof params.request !== "object" || Array.isArray(params.request))) {
    throw new Error(`${params.action} requires an object request`);
  }
  if ((params.action === "status" || params.action === "collect" || params.action === "cancel" || params.action === "integrate") && !params.runId) {
    throw new Error(`${params.action} requires runId`);
  }
  if ((params.action === "collect" || params.action === "integrate") && !params.workerId) {
    throw new Error(`${params.action} requires workerId`);
  }
  if (params.action === "cancel" && !params.reason) {
    throw new Error("cancel requires reason");
  }
}

function bounded(text: string): string {
  const bytes = Buffer.byteLength(text, "utf8");
  if (bytes <= MAX_OUTPUT_BYTES) return text;
  let result = text.slice(0, MAX_OUTPUT_BYTES);
  while (Buffer.byteLength(result, "utf8") > MAX_OUTPUT_BYTES) result = result.slice(0, -1);
  return `${result}\n[delegate output truncated; inspect the durable ledger for full details]`;
}

async function callCore(pi: ExtensionAPI, input: CoreInput, cwd: string, signal: AbortSignal | undefined) {
  const directory = await mkdtemp(join(tmpdir(), "agent-forge-delegate-"));
  const inputPath = join(directory, "request.json");
  try {
    await writeFile(inputPath, JSON.stringify(input), { encoding: "utf8", mode: 0o600 });
    const result = await pi.exec("python3", ["-m", "forge.orchestrator.cli", "--input-file", inputPath], {
      cwd,
      signal,
    });
    if (result.code !== 0) {
      throw new Error(bounded(result.stderr || result.stdout || `core exited with code ${result.code}`));
    }
    try {
      return JSON.parse(result.stdout) as Record<string, unknown>;
    } catch (error) {
      throw new Error(`core returned invalid JSON: ${error instanceof Error ? error.message : String(error)}`);
    }
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

function formatWorkers(result: Record<string, unknown>): string {
  return bounded(JSON.stringify(result, null, 2));
}

export default function (pi: ExtensionAPI) {
  let activeDigest = "";
  let latestPointers: { runIds: string[]; orphaned: string[] } = { runIds: [], orphaned: [] };

  const refreshDigest = async (cwd: string) => {
    try {
      const result = await callCore(pi, { action: "digest", maxRuns: 10 }, cwd, undefined);
      const runs = Array.isArray(result.runs) ? result.runs : [];
      const orphaned = Array.isArray(result.orphaned) ? result.orphaned : [];
      latestPointers = {
        runIds: runs
          .map((run) => (run && typeof run === "object" && "runId" in run ? String(run.runId) : ""))
          .filter(Boolean),
        orphaned: orphaned
          .map((run) => (run && typeof run === "object" && "runId" in run ? String(run.runId) : ""))
          .filter(Boolean),
      };
      activeDigest = formatWorkers(result);
    } catch (error) {
      latestPointers = { runIds: [], orphaned: ["ledger-unavailable"] };
      activeDigest = `Agent Forge run ledger unavailable; no run state was fabricated.\n${String(error)}`;
    }
  };

  pi.on("session_start", async (_event, ctx) => {
    const allowed = new Set(["read", "grep", "find", "ls", "delegate"]);
    pi.setActiveTools([...new Set([...pi.getActiveTools().filter((name) => allowed.has(name)), "delegate"])]);
    await refreshDigest(ctx.cwd);
    pi.appendEntry("agent-forge-run-pointers", latestPointers);
  });

  pi.on("session_compact", async (_event, ctx) => {
    await refreshDigest(ctx.cwd);
  });

  pi.on("before_agent_start", async (_event, ctx) => {
    if (!activeDigest) await refreshDigest(ctx.cwd);
    return {
      message: {
        customType: "agent-forge-run-digest",
        content: activeDigest,
        display: false,
        details: { bounded: true, runIds: latestPointers.runIds, orphaned: latestPointers.orphaned },
      },
    };
  });

  pi.registerCommand("workers", {
    description: "Inspect bounded Agent Forge worker status or cancel a queued run",
    handler: async (args, ctx) => {
      const parts = args.trim().split(/\s+/).filter(Boolean);
      if (parts[0] === "cancel") {
        const runId = parts[1];
        const reason = parts.slice(2).join(" ") || "cancelled by operator";
        if (!runId) {
          ctx.ui.notify("Usage: /workers cancel <runId> [reason]", "warning");
          return;
        }
        const result = await callCore(
          pi,
          { action: "cancel", runId, reason, repositoryRoot: ctx.cwd, repositoryId: "current", backend: "orca-pi" },
          ctx.cwd,
          undefined,
        );
        await refreshDigest(ctx.cwd);
        ctx.ui.notify(formatWorkers(result), "info");
        return;
      }

      let runId = parts[0];
      if (!runId && ctx.mode === "tui") {
        const index = await callCore(
          pi,
          { action: "status", repositoryRoot: ctx.cwd, repositoryId: "current", backend: "orca-pi" },
          ctx.cwd,
          undefined,
        );
        const data = index.index as { active?: Array<{ runId: string }>; recent?: Array<{ runId: string }> } | undefined;
        const choices = [...(data?.active ?? []), ...(data?.recent ?? [])].map((item) => item.runId);
        if (choices.length > 0) runId = await ctx.ui.select("Inspect worker run", choices);
      }
      const result = await callCore(
        pi,
        { action: "status", runId, repositoryRoot: ctx.cwd, repositoryId: "current", backend: "orca-pi" },
        ctx.cwd,
        undefined,
      );
      ctx.ui.notify(formatWorkers(result), "info");
    },
  });

  pi.registerTool({
    name: "delegate",
    label: "Delegate",
    description:
      "Delegate through the durable Agent Forge core. Use catalog to inspect capability summaries, preview to resolve a WorkerRequest without side effects, spawn to persist a compiled run, status/collect to inspect durable state, and cancel/integrate only when the core reports those operations available.",
    promptSnippet: "Delegate a constrained task through the durable Agent Forge conductor",
    promptGuidelines: [
      "Use delegate with action catalog before guessing available capabilities.",
      "Use delegate preview before spawn when the WorkerRequest or resolved resources need inspection.",
      "Never put shell commands, filesystem paths, MCP endpoints, credentials, or system prompts in the WorkerRequest.",
    ],
    parameters: DelegateParameters,
    async execute(_toolCallId, params, signal, onUpdate, ctx) {
      assertActionShape(params);
      onUpdate?.({
        content: [{ type: "text", text: `Running delegate action: ${params.action}` }],
        details: { action: params.action, state: "dispatching" },
      });

      const input: CoreInput = {
        action: params.action,
        request: params.request,
        runId: params.runId,
        workerId: params.workerId,
        reason: params.reason,
        // These are conductor-owned, never model-authored fields.
        repositoryRoot: ctx.cwd,
        repositoryId: "current",
        backend: "orca-pi",
      };
      const result = await callCore(pi, input, ctx.cwd, signal);
      const text = bounded(JSON.stringify(result, null, 2));
      onUpdate?.({
        content: [{ type: "text", text }],
        details: { action: params.action, state: result.status ?? "complete" },
      });
      return {
        content: [{ type: "text", text }],
        details: { action: params.action, result },
      };
    },
  });
}
