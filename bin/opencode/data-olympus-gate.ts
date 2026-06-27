// data-olympus-enforce (managed) v1 -- installed by `kb enforce install --agent opencode`.
// Gates file-mutating tools through the data-olympus gate. Remove via
// `kb enforce uninstall --agent opencode`.
import type { Plugin, Hooks } from "@opencode-ai/plugin"

const ENDPOINT = process.env.KB_ENDPOINT ?? "http://localhost:8080"
const FAIL_MODE = process.env.KB_ENFORCE_FAIL_MODE ?? "open"
const TOKEN = process.env.KB_AUTH_TOKEN ?? ""
// Gate mutating tools. "bash" is included because shell-driven writes surface as bash.
const GATED = new Set(["edit", "write", "patch", "multiedit", "bash"])

export const DataOlympusGate: Plugin = async ({ directory, worktree }) => {
  return {
    "tool.execute.before": async (input, output) => {
      if (!GATED.has(input.tool)) return
      const headers: Record<string, string> = { "content-type": "application/json" }
      if (TOKEN) headers["authorization"] = `Bearer ${TOKEN}`
      let verdict: string | undefined
      try {
        const res = await fetch(`${ENDPOINT}/api/v1/gate/check`, {
          method: "POST",
          headers,
          body: JSON.stringify({
            workspace: worktree ?? directory,
            session_id: input.sessionID,
            tool_name: input.tool,
            action_path: (output.args && (output.args.filePath ?? output.args.path)) ?? "",
          }),
          signal: AbortSignal.timeout(5000),
        })
        if (!res.ok) {
          if (FAIL_MODE === "closed")
            throw new Error(`data-olympus gate HTTP ${res.status}; blocking (fail-closed)`)
          return
        }
        verdict = ((await res.json()) as { verdict?: string }).verdict
      } catch (err) {
        if (err instanceof Error && err.message.startsWith("data-olympus gate")) throw err
        if (FAIL_MODE === "closed")
          throw new Error(`data-olympus gate unreachable; blocking (fail-closed)`)
        return // fail open
      }
      if (verdict === "consult_required") {
        throw new Error(
          `BLOCKED by data-olympus: '${input.tool}' requires KB consultation. ` +
          `Call the kb_consult MCP tool for this workspace, then retry.`,
        )
      }
    },
  } satisfies Hooks
}
