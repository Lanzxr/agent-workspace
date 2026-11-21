/**
 * Local SearXNG bridge search provider for headless DSH workers.
 *
 * Calls Company Agent's loopback web gate (`POST /internal/web/dsh-search`)
 * inside DSH's Node host process so bash children remain `--unshare-net` and
 * never receive bridge credentials.
 */
export const name = "company-agent-searxng";
export const inject = ["web"];

const SEARCH_PATH = "/internal/web/dsh-search";

function resolveSearchEndpoint(rawUrl) {
  const value = (rawUrl || "").trim();
  if (!value) return "";
  try {
    const parsed = new URL(value);
    if (parsed.pathname === SEARCH_PATH) {
      return parsed.toString();
    }
    return new URL(SEARCH_PATH, parsed.origin).toString();
  } catch {
    return "";
  }
}

function getCredentials() {
  const endpoint = resolveSearchEndpoint(process.env.COMPANY_AGENT_WEB_GATE_URL);
  const apiKey = (process.env.COMPANY_AGENT_WEB_GATE_API_KEY || "").trim();
  return { endpoint, apiKey };
}

function mapSource(item) {
  if (!item || typeof item !== "object" || typeof item.url !== "string" || !item.url) {
    return null;
  }
  const source = { url: item.url };
  if (typeof item.title === "string" && item.title) source.title = item.title;
  if (typeof item.snippet === "string" && item.snippet) source.snippet = item.snippet;
  if (typeof item.summary === "string" && item.summary) source.summary = item.summary;
  if (typeof item.publishedAt === "string" && item.publishedAt) source.publishedAt = item.publishedAt;
  return source;
}

export function apply(ctx) {
  ctx.web.registerSearchProvider({
    id: "company-agent-searxng",
    available() {
      const { endpoint, apiKey } = getCredentials();
      return Boolean(endpoint && apiKey);
    },
    ready() {
      const { endpoint, apiKey } = getCredentials();
      return Boolean(endpoint && apiKey);
    },
    async search(request, signal) {
      const { endpoint, apiKey } = getCredentials();
      if (!endpoint || !apiKey) {
        throw new Error("SearXNG web gate endpoint or API key is not configured");
      }
      const query = typeof request?.query === "string" ? request.query.trim() : "";
      if (!query) {
        throw new Error("search query must not be empty");
      }
      const maxResults = Number.isInteger(request?.maxResults)
        ? Math.max(1, Math.min(request.maxResults, 8))
        : 8;
      const response = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${apiKey}`,
        },
        body: JSON.stringify({ query, max_results: maxResults }),
        signal,
      });
      if (!response.ok) {
        let message = `SearXNG bridge failed (${response.status})`;
        try {
          const errorBody = await response.json();
          if (errorBody && typeof errorBody.error === "string" && errorBody.error) {
            message = `SearXNG bridge error (${response.status}): ${errorBody.error}`;
          }
        } catch {
          // ignore non-JSON error body
        }
        throw new Error(message);
      }
      const payload = await response.json();
      const rawSources = Array.isArray(payload?.sources) ? payload.sources : [];
      const sources = rawSources.map(mapSource).filter(Boolean);
      const result = {
        sources,
        truncated: Boolean(payload?.truncated),
      };
      if (typeof payload?.content === "string" && payload.content.length > 0) {
        result.content = payload.content;
      }
      return result;
    },
  });
}
