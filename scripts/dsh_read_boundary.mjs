import { realpathSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";

export const name = "grounded-build-dsh-read-boundary";
export const inject = ["tools"];

const READ_TOOLS = new Map([
  ["read", "file_path"],
  ["read_image", "file_path"],
  ["glob", "path"],
  ["grep", "path"],
]);

const WEB_TOOLS = new Set(["web", "web_search", "web_fetch", "x_search", "read_page"]);

function contains(root, candidate) {
  const suffix = relative(root, candidate);
  return suffix === "" || (!suffix.startsWith("..") && !isAbsolute(suffix));
}

export function apply(ctx, config) {
  const allowedRoots = config.allowedRoots.map((root) => realpathSync(root));
  const workspaceRoot = realpathSync(config.workspaceRoot);

  ctx.on("tools/pre-execute", (execution, next) => {
    if (config.allowWeb === true && WEB_TOOLS.has(execution.name)) return next();
    const argumentName = READ_TOOLS.get(execution.name);
    if (argumentName === undefined) {
      return Promise.resolve({
        kind: "deny",
        reason: "grounded-build DSH review permits only its explicit read-tool allowlist",
      });
    }
    const args = execution.arguments ?? {};
    if (execution.name === "glob") {
      const pattern = args.pattern;
      if (typeof pattern !== "string" || isAbsolute(pattern) || pattern.split("/").includes("..")) {
        return Promise.resolve({ kind: "deny", reason: "grounded-build denied an unsafe glob pattern" });
      }
    }
    const requested = args[argumentName] ?? workspaceRoot;
    if (typeof requested !== "string") {
      return Promise.resolve({ kind: "deny", reason: "grounded-build denied an invalid file path" });
    }
    let candidate;
    try {
      candidate = realpathSync(isAbsolute(requested) ? requested : resolve(workspaceRoot, requested));
    } catch {
      return Promise.resolve({ kind: "deny", reason: "grounded-build denied an unresolved file path" });
    }
    if (allowedRoots.some((root) => contains(root, candidate))) return next();
    return Promise.resolve({
      kind: "deny",
      reason: "grounded-build permits DSH file reads only inside the frozen worktree and invocation context",
    });
  }, { prepend: true });
}
