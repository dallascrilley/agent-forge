import { spawn } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type, type Static } from "typebox";

const MAX_INPUT_BYTES = 128 * 1024;
const MAX_OUTPUT_BYTES = 50 * 1024;

const SubmitParameters = Type.Object(
  {
    taskId: Type.String({ minLength: 1, description: "Exact Task ID from the live Orca preamble." }),
    dispatchId: Type.String({ minLength: 1, description: "Exact Dispatch ID from the live Orca preamble." }),
    result: Type.Any({ description: "Complete WorkerResult v1 object for this worker." }),
  },
  { additionalProperties: false },
);

type SubmitParams = Static<typeof SubmitParameters>;

async function callSubmissionCore(
  input: SubmitParams,
  cwd: string,
  signal: AbortSignal | undefined,
): Promise<Record<string, unknown>> {
  const encoded = JSON.stringify(input);
  if (Buffer.byteLength(encoded, "utf8") > MAX_INPUT_BYTES) {
    throw new Error(`WorkerResult exceeds ${MAX_INPUT_BYTES} bytes`);
  }
  return await new Promise((resolve, reject) => {
    const child = spawn("python3", ["-m", "forge.orchestrator.worker_submit"], {
      cwd,
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let outputBytes = 0;
    const collect = (target: Buffer[]) => (chunk: Buffer) => {
      outputBytes += chunk.length;
      if (outputBytes > MAX_OUTPUT_BYTES) {
        child.kill();
        reject(new Error("worker submission core output exceeded its bound"));
        return;
      }
      target.push(chunk);
    };
    child.stdout.on("data", collect(stdout));
    child.stderr.on("data", collect(stderr));
    child.on("error", reject);
    const abort = () => child.kill("SIGTERM");
    signal?.addEventListener("abort", abort, { once: true });
    child.on("close", (code) => {
      signal?.removeEventListener("abort", abort);
      if (signal?.aborted) {
        reject(new Error("worker result submission cancelled"));
        return;
      }
      const out = Buffer.concat(stdout).toString("utf8");
      const err = Buffer.concat(stderr).toString("utf8");
      if (code !== 0) {
        reject(new Error(err || out || `worker submission core exited with code ${code}`));
        return;
      }
      try {
        resolve(JSON.parse(out) as Record<string, unknown>);
      } catch (error) {
        reject(new Error(`worker submission core returned invalid JSON: ${String(error)}`));
      }
    });
    child.stdin.end(encoded);
  });
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "submit_worker_result",
    label: "Submit Worker Result",
    description:
      "Validate and durably submit the final structured WorkerResult for this supervised worker. This is the only permitted result write and lifecycle action.",
    promptSnippet: "Submit the final WorkerResult and settle the supervised worker exactly once",
    promptGuidelines: [
      "Use submit_worker_result exactly once as the final action after gathering repository evidence.",
      "Pass the exact taskId and dispatchId from the live Orca preamble to submit_worker_result.",
      "Do not emit a second assistant response after submit_worker_result succeeds.",
    ],
    parameters: SubmitParameters,
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      const receipt = await callSubmissionCore(params, ctx.cwd, signal);
      return {
        content: [{ type: "text", text: `Worker result ${String(receipt.state ?? "submitted")}.` }],
        details: receipt,
        terminate: true,
      };
    },
  });
}
