// data-olympus-enforce (managed) v1 -- installed by `kb enforce install --agent opencode`.
// Gates file-mutating tools through the data-olympus gate. Remove via
// `kb enforce uninstall --agent opencode`.
import { execFileSync } from "node:child_process"
import { basename } from "node:path"
import type { Plugin, Hooks } from "@opencode-ai/plugin"

const ENDPOINT = process.env.KB_ENDPOINT ?? "http://localhost:8080"
const FAIL_MODE = process.env.KB_ENFORCE_FAIL_MODE ?? "open"
const TOKEN = process.env.KB_AUTH_TOKEN ?? ""
// Gate mutating tools. "bash" is included because shell-driven writes surface as bash.
const GATED = new Set(["edit", "write", "patch", "multiedit", "bash"])

// Resolve the workspace key the SAME way bin/kb-enforce-hook's resolve_workspace
// does: the MAIN git worktree basename (worktree-invariant, so a consult recorded
// from the main checkout clears the gate from any linked worktree too), matching
// the basename key used by every other surface. Falls back to the basename of the
// worktree/directory path, then the raw path, so the key is never empty.
// Previously this sent the raw absolute `worktree ?? directory` path, which could
// never match consults recorded under the basename key everywhere else.
export function resolveWorkspace(dir: string): string {
  try {
    const out = execFileSync("git", ["-C", dir, "worktree", "list", "--porcelain"], {
      encoding: "utf-8",
      stdio: ["ignore", "pipe", "ignore"],
    })
    // First NON-bare worktree record; the main worktree is listed first. Skip a
    // leading bare record (a bare repo's first entry is the bare git dir).
    let path: string | undefined
    let bare = false
    for (const line of out.split("\n")) {
      if (line.startsWith("worktree ")) {
        path = line.slice("worktree ".length)
        bare = false
      } else if (line === "bare") {
        bare = true
      } else if (line === "") {
        if (path && !bare) return basename(path)
        path = undefined
        bare = false
      }
    }
    if (path && !bare) return basename(path)
  } catch {
    // not a git repo / git unavailable -> fall through to basename of the path
  }
  return basename(dir) || dir
}

export const DataOlympusGate: Plugin = async ({ directory, worktree }) => {
  const workspace = resolveWorkspace(worktree ?? directory)
  return {
    "tool.execute.before": async (input, output) => {
      if (!GATED.has(input.tool)) return
      const headers: Record<string, string> = { "content-type": "application/json" }
      if (TOKEN) headers["authorization"] = `Bearer ${TOKEN}`
      // action_diff: the change content, so the gate classifier sees a real signal
      // for bash (the command) and patch (the diff) which carry no file path.
      // Capped at 4000 chars to bound the request body (mirrors kb-enforce-hook).
      const args = output.args ?? {}
      const rawDiff =
        (args.content ?? args.newString ?? args.new_string ?? args.command ?? args.patch ?? "") as string
      const actionDiff = typeof rawDiff === "string" ? rawDiff.slice(0, 4000) : ""
      let verdict: string | undefined
      try {
        const res = await fetch(`${ENDPOINT}/api/v1/gate/check`, {
          method: "POST",
          headers,
          body: JSON.stringify({
            workspace,
            session_id: input.sessionID,
            tool_name: input.tool,
            action_path: (args.filePath ?? args.path) ?? "",
            action_diff: actionDiff,
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
        // Actionable deny: echo the exact workspace key and session id (the one
        // value the agent cannot guess) so the clearing call is copy-pasteable.
        throw new Error(
          `BLOCKED by data-olympus: '${input.tool}' is a governed change with no ` +
          `explicit consultation on record. Call ` +
          `kb_consult(workspace='${workspace}', source_session='${input.sessionID}', ` +
          `intent='<what you are doing>') then retry.`,
        )
      }
    },
  } satisfies Hooks
}
