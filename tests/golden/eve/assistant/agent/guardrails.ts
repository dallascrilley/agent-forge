/**
 * Mechanical guardrails for the generated Eve agent.
 *
 * Every tool/side-effect entry point must call checkStopFile, checkTool,
 * requireSideEffect, and writeReceipt through runGuarded in agent.ts.
 */
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

const CONFIG = {
  "stopFile": "doc-assistant.stop",
  "allowedTools": [
    "read_file",
    "read_text_file",
    "list_directory",
    "search_files"
  ],
  "allowedSideEffects": [],
  "maxActions": 0,
  "receipt": "receipts/doc-assistant-last.json"
} as const;

function matches(pattern: string, action: string): boolean {
  return pattern.endsWith("/")
    ? action.startsWith(pattern)
    : pattern === action;
}

class Guardrails {
  private used = 0;

  checkStopFile(): void {
    if (existsSync(resolve(process.cwd(), CONFIG.stopFile))) {
      this.writeReceipt("paused", "stop file present");
      throw new Error(`guardrails: ${CONFIG.stopFile} is present`);
    }
  }

  checkTool(name: string): void {
    if (
      CONFIG.allowedTools !== null &&
      !CONFIG.allowedTools.some((pattern) => matches(pattern, name))
    ) {
      throw new Error(`guardrails: tool ${name} is not allowlisted`);
    }
  }

  requireSideEffect(action: string): void {
    if (!CONFIG.allowedSideEffects.some((pattern) => matches(pattern, action))) {
      throw new Error(`guardrails: side effect ${action} is not allowed`);
    }
    if (this.used >= CONFIG.maxActions) {
      throw new Error(`guardrails: action budget (${CONFIG.maxActions}) exhausted`);
    }
    this.used += 1;
  }

  writeReceipt(verdict: "ok" | "blocked" | "paused", note: string): void {
    const target = resolve(process.cwd(), CONFIG.receipt);
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(
      target,
      JSON.stringify({ verdict, note, actions: this.used }, null, 2) + "\n",
      "utf8",
    );
  }
}

export const GUARDRAILS = new Guardrails();
