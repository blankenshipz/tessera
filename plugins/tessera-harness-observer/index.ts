import { Plugin } from "@opencode/plugin"
import { Rpc } from "@opencode/plugin/rpc"
import { createHash } from "node:crypto"

// No config/credential resolution, filesystem, provider traffic, messages or inference.
export const protocol = "tessera-harness-observer-v1"
const gaps = [
  "effective_config_safe_runtime_projection_unavailable",
  "selected_skill_raw_bytes_and_activation_unavailable",
  "stable_harness_instance_identity_unavailable",
  "installed_build_and_mcp_sdk_identity_unavailable",
  "active_included_oauth_route_and_no_paid_fallback_unavailable",
  "model_snapshot_final_hook_order_unproven",
]
const string = { type: "string", minLength: 1, maxLength: 512 } as const
const object = (properties: Record<string, unknown>) => ({
  type: "object", properties, required: Object.keys(properties), additionalProperties: false,
})
const array = (items: unknown, maxItems = 128) => ({ type: "array", items, maxItems })
const catalogItem = object({ name: string, description: { type: "string", maxLength: 8192 },
  inputSchema: { type: "object" } })
const model = object({ providerID: string, id: string })
export const ObserverRpc = Rpc.define({
  id: "tessera.harness.observer.v1",
  methods: { observe: {
    input: object({ protocol: { const: protocol, type: "string" }, nonce: string, sessionID: string }),
    output: object({ protocol: { const: protocol, type: "string" }, nonce: string,
      sessionID: string, workspace: string, harnessVersion: string,
      registeredCatalog: array(catalogItem),
      registeredSkill: object({ id: string, bodySha256: string }),
      selectedModel: model,
      activeConnection: { anyOf: [{ type: "null" }, object({ type: { const: "credential", type: "string" },
        id: string, method: { enum: ["oauth", "key"], type: "string" }, needsAuth: { type: "boolean" } }),
        object({ type: { const: "env", type: "string" }, needsAuth: { type: "boolean" } })] },
      snapshot: { anyOf: [{ type: "null" }, object({ model, catalog: array(catalogItem),
        sequence: { type: "integer", minimum: 1 }, capturedAt: string })] },
      gaps: array(string),
    }),
    errors: { blocked: object({ reason: { const: "observation_unavailable", type: "string" } }) },
  }, produce: {
    input: object({ protocol: { const: protocol, type: "string" }, nonce: string, sessionID: string }),
    output: object({ protocol: { const: "tessera-runtime-guard-partial-v1", type: "string" },
      complete: { const: false, type: "boolean" }, nonce: string, sessionID: string,
      issuedAt: string, expiresAt: string, publicObservation: { type: "object" },
      agentPermissions: array(object({ action: string, resource: string,
        effect: { enum: ["allow", "deny", "ask"], type: "string" } })),
      providerMetadata: object({ id: string, integrationID: string, package: string }),
      transportObservations: array(object({ hook: string, origin: string, method: string,
        model, kind: string, capturedAt: string })),
      sources: array(string), missing: array(string),
    }),
    errors: { blocked: object({ reason: { const: "observation_unavailable", type: "string" } }) },
  } },
})

function bounded(value: unknown, max = 131072) {
  const text = JSON.stringify(value)
  if (!text || Buffer.byteLength(text) > max) throw new Error("bound")
  return JSON.parse(text)
}
function text(value: unknown, max = 512): string {
  if (typeof value !== "string" || !value || value.length > max || /[\x00-\x1f\x7f]/.test(value))
    throw new Error("shape")
  return value
}
function jsonSchema(value: unknown) {
  // Tool.Info may carry Standard Schema. Do not pretend it is JSON Schema.
  if (!value || typeof value !== "object" || Array.isArray(value) || "~standard" in value)
    throw new Error("unsupported_schema")
  return bounded(value, 32768)
}
function description(value: unknown) {
  if (typeof value !== "string" || Buffer.byteLength(value) > 8192) throw new Error("description")
  return value
}
function catalog(tools: readonly any[], permitted: string[]) {
  const rows = tools.filter(tool => typeof tool.id === "string" && tool.id.startsWith("tessera_"))
    .map(tool => ({ name: text(tool.id), description: description(tool.description),
      inputSchema: jsonSchema(tool.input) }))
    .sort((a, b) => a.name.localeCompare(b.name))
  if (rows.length !== permitted.length || rows.some((row, i) => row.name !== permitted[i]))
    throw new Error("catalog")
  return bounded(rows)
}
function connection(value: any) {
  if (value === undefined) return null
  if (!value || typeof value !== "object") throw new Error("connection")
  if (value.status && value.status.status !== "needs_auth") throw new Error("connection")
  const needsAuth = value.status !== undefined
  // Whitelist metadata; never copy label, status message/url, credentials or tokens.
  if (value.type === "credential" && ["oauth", "key"].includes(value.method))
    return { type: "credential", id: text(value.id), method: value.method, needsAuth }
  if (value.type === "env") return { type: "env", needsAuth }
  throw new Error("connection")
}

export default Plugin.define({
  id: "tessera-harness-observer",
  async setup(ctx) {
    // Options are expected enrollment constraints, NEVER observed proof of admission.
    const options = ctx.options as Record<string, unknown>
    if (options.dedicatedResearch !== true || ctx.app.version !== "2.0.18"
        || ctx.location.directory !== options.workspace) throw new Error("observer_installation_mismatch")
    const sessionID = text(options.sessionID)
    const agent = text(options.agent)
    const skillID = text(options.skillID)
    const providerID = text(options.providerID)
    const modelID = text(options.modelID)
    const integrationID = text(options.integrationID)
    const allowedOrigins = Array.isArray(options.allowedTransportOrigins)
      ? options.allowedTransportOrigins.map(value => {
        const url = new URL(text(value))
        if (!["https:", "wss:"].includes(url.protocol) || url.username || url.password
            || url.search || url.hash || url.pathname !== "/") throw new Error("unsafe_origin")
        return url.origin
      }) : []
    const transportObservations: any[] = []
    if (!Array.isArray(options.permittedTools) || options.permittedTools.length < 1
        || options.permittedTools.length > 128) throw new Error("observer_installation_mismatch")
    const permitted = options.permittedTools.map(value => text(value)).sort()
    if (new Set(permitted).size !== permitted.length || permitted.some(name => !name.startsWith("tessera_")))
      throw new Error("observer_installation_mismatch")
    let sequence = 0
    let snapshot: any = null
    const hook = await ctx.session.hook("context", event => {
      if (event.sessionID !== sessionID) return
      snapshot = null
      if (event.agent !== agent || event.model.providerID !== providerID || event.model.id !== modelID)
        return
      try {
        // Only schemas/model/session metadata. Never traverse system/messages/options.
        const tools = Object.entries(event.tools as Record<string,
          { description: string; input: unknown }>).map(([id, tool]) => ({ id, ...tool }))
        // Request tools must contain precisely the allowed names, not a filtered subset.
        if (tools.length !== permitted.length) return
        snapshot = { model: { providerID: event.model.providerID, id: event.model.id },
          catalog: catalog(tools, permitted), sequence: ++sequence, capturedAt: new Date().toISOString() }
      } catch { snapshot = null }
    })
    const hooks = [hook]
    const observeTransport = (name: string, event: any, urlValue: unknown, method: string) => {
      if (event.sessionID !== sessionID) return
      // Never read headers/body/frames; reject userinfo/insecure/unknown origins.
      try {
        const url = new URL(text(urlValue, 8192))
        if (url.username || url.password || !["https:", "wss:"].includes(url.protocol)
            || !allowedOrigins.includes(url.origin) || event.model.providerID !== providerID
            || event.model.id !== modelID) return
        transportObservations.push({ hook: name, origin: url.origin, method,
          model: { providerID: event.model.providerID, id: event.model.id },
          kind: text(event.kind), capturedAt: new Date().toISOString() })
        if (transportObservations.length > 16) transportObservations.shift()
      } catch { /* Unknown route remains unproven; never log raw URL or input. */ }
    }
    let registration
    try {
      hooks.push(await ctx.session.hook("model.request", event =>
        observeTransport("model.request", event, event.baseURL, "unspecified")))
      hooks.push(await ctx.session.hook("http.request", event =>
        observeTransport("http.request", event, event.request.url, text(event.request.method))))
      hooks.push(await ctx.session.hook("experimental.ws.handshake", event =>
        observeTransport("experimental.ws.handshake", event, event.url, "WEBSOCKET")))
      const readPublic = async (input: unknown, rpc: any) => {
        try {
          const request = input as Record<string, unknown>
          if (Object.keys(request).sort().join(",") !== "nonce,protocol,sessionID"
              || request.protocol !== protocol || request.sessionID !== sessionID) throw new Error("request")
          const nonce = text(request.nonce)
          const current = await ctx.session.get({ sessionID: sessionID as any })
          if (current.id !== sessionID || current.location.directory !== ctx.location.directory
              || current.agent !== agent || current.model?.providerID !== providerID || current.model.id !== modelID)
            throw new Error("session")
          const registeredCatalog = catalog(await ctx.tool.list(), permitted)
          const skills = await ctx.skill.list()
          if (skills.location.directory !== ctx.location.directory) throw new Error("skill_location")
          const selected = skills.data.filter(skill => skill.id === skillID)
          if (selected.length !== 1 || typeof selected[0].content !== "string"
              || Buffer.byteLength(selected[0].content) > 32768) throw new Error("skill")
          const activeConnection = connection(await ctx.integration.connection.active(integrationID))
          const currentGaps = [...gaps]
          if (!snapshot) currentGaps.push("model_request_snapshot_not_observed")
          if (!activeConnection || activeConnection.type !== "credential" || activeConnection.method !== "oauth"
              || activeConnection.needsAuth) currentGaps.push("active_oauth_connection_not_observed")
          return bounded({ protocol, nonce, sessionID, workspace: ctx.location.directory,
            harnessVersion: ctx.app.version, registeredCatalog,
            registeredSkill: { id: selected[0].id,
              bodySha256: createHash("sha256").update(selected[0].content).digest("hex") },
            selectedModel: { providerID: current.model.providerID, id: current.model.id },
            activeConnection, snapshot, gaps: currentGaps })
        } catch { return rpc.error("blocked", "Observation unavailable", { reason: "observation_unavailable" }) }
      }
      registration = await ctx.rpc.register(ObserverRpc, { observe: readPublic,
        produce: async (input, rpc) => {
          try {
            const publicObservation = await readPublic(input, rpc)
            if (publicObservation.protocol !== protocol) throw new Error("partial")
            const agentInfo = await ctx.agent.get({ agentID: agent as any })
            if (agentInfo.location.directory !== ctx.location.directory || agentInfo.data.id !== agent)
              throw new Error("agent")
            const agentPermissions = agentInfo.data.permissions.map((rule: any) => ({
              action: text(rule.action), resource: text(rule.resource), effect: text(rule.effect) }))
            if (agentPermissions.some(rule => !["allow", "deny", "ask"].includes(rule.effect)))
              throw new Error("permissions")
            const provider = await ctx.provider.get({ providerID: providerID as any })
            // Provider.Info contains unsafe settings/headers/body; never traverse/copy them.
            const providerMetadata = { id: text(provider.data.id),
              integrationID: text(provider.data.integrationID), package: text(provider.data.package) }
            if (provider.location.directory !== ctx.location.directory || providerMetadata.id !== providerID
                || providerMetadata.integrationID !== integrationID) throw new Error("provider")
            const issuedAt = new Date().toISOString()
            const fresh = transportObservations.filter(item => Date.now() - Date.parse(item.capturedAt) <= 30000)
            return bounded({ protocol: "tessera-runtime-guard-partial-v1", complete: false,
              nonce: publicObservation.nonce, sessionID, issuedAt,
              expiresAt: new Date(Date.parse(issuedAt) + 30000).toISOString(), publicObservation,
              agentPermissions, providerMetadata, transportObservations: fresh,
              sources: ["ctx.tool.list", "ctx.skill.list", "ctx.agent.get", "ctx.provider.get.metadata",
                "ctx.session.get", "ctx.integration.connection.active.metadata", "session.context.point",
                "session.transport-hooks.point"],
              missing: [...publicObservation.gaps, "provider_context_provenance_not_current_dispatch",
                "agent_permissions_not_complete_effective_session_policy", "transport_hooks_not_final_dispatch"] })
          } catch { return rpc.error("blocked", "Observation unavailable", { reason: "observation_unavailable" }) }
        },
      })
    } catch (error) { for (const item of hooks) await item.dispose(); throw error }
    return async () => {
      snapshot = null; transportObservations.length = 0; await registration.dispose()
      for (const item of hooks) await item.dispose()
    }
  },
})
