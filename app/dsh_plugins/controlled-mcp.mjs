/**
 * Controlled MCP client bridge for headless DSH workers.
 *
 * Keeps secrets out of provider.patch.yml and workspace files by resolving
 * envRefs / headerEnvRefs strictly from the DSH host process environment in
 * memory before delegating to @deepseek-ai/dsh-mcp-client.
 */
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

export const name = "company-agent-mcp";
export const inject = ["tools"];

const ENV_TEMPLATE_RE = /\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g;
const WSL_BRIDGE_HOSTS = new Set(["wsl-host", "host.wsl.internal"]);

export function resolveWslDefaultGateway(routeTablePath = "/proc/net/route") {
  try {
    const content = fs.readFileSync(routeTablePath, "utf8");
    for (const line of content.split(/\r?\n/).slice(1)) {
      const cols = line.trim().split(/\s+/);
      if (cols.length >= 3 && cols[1] === "00000000" && /^[0-9A-Fa-f]{8}$/.test(cols[2]) && cols[2] !== "00000000") {
        const hex = cols[2];
        const octets = [6, 4, 2, 0].map((idx) => parseInt(hex.slice(idx, idx + 2), 16));
        return octets.join(".");
      }
    }
  } catch {
    // Not Linux / no route table
  }
  return "127.0.0.1";
}

export function resolveBridgeHostUrl(rawUrl, routeTablePath = "/proc/net/route") {
  const value = typeof rawUrl === "string" ? rawUrl.trim() : "";
  if (!value) return "";
  try {
    const parsed = new URL(value);
    if (WSL_BRIDGE_HOSTS.has(parsed.hostname.toLowerCase())) {
      parsed.hostname = resolveWslDefaultGateway(routeTablePath);
      return parsed.toString();
    }
  } catch {
    // Let downstream validation catch malformed URLs
  }
  return value;
}

async function loadMcpClientModule(explicitPath) {
  const candidates = [];
  if (typeof explicitPath === "string" && explicitPath.trim()) {
    candidates.push(pathToFileURL(explicitPath.trim()).href);
  }
  if (typeof process.argv[1] === "string" && process.argv[1].trim()) {
    const sibling = path.resolve(path.dirname(process.argv[1]), "../../dsh-mcp-client/lib/index.js");
    candidates.push(pathToFileURL(sibling).href);
  }
  for (const href of candidates) {
    try {
      return await import(href);
    } catch {
      // try next candidate
    }
  }
  return await import("@deepseek-ai/dsh-mcp-client");
}

function readHostEnv(varName) {
  const value = process.env[varName];
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`controlled-mcp: missing host environment variable ${varName}`);
  }
  return value.trim();
}

function resolveEnvRefs(baseEnv, envRefs) {
  const resolved = { ...(baseEnv && typeof baseEnv === "object" ? baseEnv : {}) };
  if (envRefs && typeof envRefs === "object") {
    for (const [targetVar, hostVar] of Object.entries(envRefs)) {
      resolved[targetVar] = readHostEnv(String(hostVar));
    }
  }
  return resolved;
}

function resolveHeaderRefs(baseHeaders, headerEnvRefs) {
  const resolved = { ...(baseHeaders && typeof baseHeaders === "object" ? baseHeaders : {}) };
  if (headerEnvRefs && typeof headerEnvRefs === "object") {
    for (const [headerName, template] of Object.entries(headerEnvRefs)) {
      resolved[headerName] = String(template).replace(ENV_TEMPLATE_RE, (_full, varName) => readHostEnv(varName));
    }
  }
  return resolved;
}

export async function apply(ctx, config) {
  const serverName = typeof config?.serverName === "string" ? config.serverName : "mcp";
  let mcpClient;
  try {
    mcpClient = await loadMcpClientModule(config?.mcpClientModulePath);
  } catch (error) {
    if (config?.failOnStartupError) {
      throw error;
    }
    ctx.logger?.warn?.(`controlled-mcp(${serverName}): @deepseek-ai/dsh-mcp-client unavailable (${String(error)})`);
    return;
  }

  let resolvedRaw;
  try {
    resolvedRaw = {
      serverName,
      transport: config?.transport || "stdio",
      toolCallTimeoutMs: config?.toolCallTimeoutMs ?? 60000,
      failOnStartupError: Boolean(config?.failOnStartupError),
    };
    if (resolvedRaw.transport === "stdio") {
      resolvedRaw.command = config?.command || "";
      resolvedRaw.args = Array.isArray(config?.args) ? config.args : [];
      resolvedRaw.cwd = typeof config?.cwd === "string" ? config.cwd : "";
      resolvedRaw.env = resolveEnvRefs(config?.env, config?.envRefs);
    } else {
      resolvedRaw.url = resolveBridgeHostUrl(config?.url || "");
      resolvedRaw.headers = resolveHeaderRefs(config?.headers, config?.headerEnvRefs);
    }
  } catch (error) {
    if (config?.failOnStartupError) {
      throw error;
    }
    ctx.logger?.warn?.(`controlled-mcp(${serverName}): skipped due to missing credential env (${error?.message || String(error)})`);
    return;
  }

  const parsedConfig = typeof mcpClient.Config === "function" ? mcpClient.Config(resolvedRaw) : resolvedRaw;
  await mcpClient.apply(ctx, parsedConfig);
}
