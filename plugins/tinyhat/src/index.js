import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";

const METADATA_BASE_URL_KEY = "tinyhat-platform-base-url";
const METADATA_AUDIENCE_KEY = "tinyhat-backend-audience";
const DEV_RUNTIME_BEARER = "dev-runtime";

const configSchema = {
  type: "object",
  properties: {
    platformBaseUrl: { type: "string" },
    backendAudience: { type: "string" },
    devMode: { type: "boolean" },
    devBearer: { type: "string" },
  },
  additionalProperties: false,
};

const emptyParameters = {
  type: "object",
  properties: {},
  additionalProperties: false,
};

const requestSecretParameters = {
  type: "object",
  required: ["name"],
  properties: {
    name: {
      type: "string",
      description: "Env-style secret name, for example OPENAI_API_KEY.",
    },
    hint: {
      type: "string",
      description: "Deprecated alias for description.",
    },
    description: {
      type: "string",
      description:
        "Optional plain-language description of what this secret is for. " +
        "Infer it from the user's request or surrounding conversation.",
    },
  },
  additionalProperties: false,
};

const terminalLinkParameters = {
  type: "object",
  properties: {
    command: {
      type: "string",
      description:
        "Optional command to show for admin approval before the terminal opens. " +
        "Do not include secret values.",
    },
  },
  additionalProperties: false,
};

const commandParameters = {
  type: "object",
  properties: {
    command: {
      type: "string",
      description: "Raw slash-command args.",
    },
    commandName: { type: "string" },
    skillName: { type: "string" },
  },
  additionalProperties: false,
};

const plugin = defineToolPlugin({
  id: "tinyhat",
  name: "Tinyhat",
  description: "Tinyhat Computer credential tools for hosted OpenClaw agents.",
  configSchema,
  tools: (tool) => [
    tool({
      name: "tinyhat_list_runtime_secrets",
      description:
        "List metadata for runtime secrets assigned to this Tinyhat Computer. " +
        "Returns names/status/revisions only, never secret values.",
      parameters: emptyParameters,
      execute: async (_params, config, context) => {
        const runtime = resolveExecutionRuntime(config, context);
        runtime.signal?.throwIfAborted?.();
        return callTinyhat(
          runtime.config,
          "/hapi/v1/computers/me/runtime-secret-statuses",
          { method: "GET" },
          runtime.signal,
        );
      },
    }),
    tool({
      name: "tinyhat_request_runtime_secret",
      description:
        "Create a Telegram Mini App link/button payload so the owner can add " +
        "or replace one runtime secret value outside chat.",
      parameters: requestSecretParameters,
      execute: async ({ name, hint, description }, config, context) => {
        const runtime = resolveExecutionRuntime(config, context);
        runtime.signal?.throwIfAborted?.();
        return callTinyhat(
          runtime.config,
          "/hapi/v1/computers/me/runtime-secrets/add-link",
          {
            method: "POST",
            body: JSON.stringify({ name, hint, description }),
          },
          runtime.signal,
        );
      },
    }),
    tool({
      name: "tinyhat_open_manage_computer_link",
      description:
        "Create a Telegram Mini App link/button payload so an agent admin can " +
        "open Manage Computer for this Computer.",
      parameters: emptyParameters,
      execute: async (_params, config, context) => {
        const runtime = resolveExecutionRuntime(config, context);
        runtime.signal?.throwIfAborted?.();
        return fetchManageComputerLink(runtime.config, runtime.signal);
      },
    }),
    tool({
      name: "tinyhat_open_terminal_link",
      description:
        "Create a Telegram Mini App link/button payload so an agent admin can " +
        "open a secure terminal for this Computer. Optionally include a command " +
        "that the admin must review and approve in the Mini App before it runs.",
      parameters: terminalLinkParameters,
      execute: async ({ command } = {}, config, context) => {
        const runtime = resolveExecutionRuntime(config, context);
        runtime.signal?.throwIfAborted?.();
        return fetchTerminalLink(runtime.config, runtime.signal, command);
      },
    }),
    tool({
      name: "tinyhat_secret_command",
      description:
        "Slash-command dispatcher for /tinyhat_secrets. Supports list/manage and add NAME.",
      parameters: commandParameters,
      execute: async ({ command = "" }, config, context) => {
        const runtime = resolveExecutionRuntime(config, context);
        runtime.signal?.throwIfAborted?.();
        const parsed = parseSecretCommand(command);
        if (parsed.action === "help") {
          return {
            ok: false,
            usage: [
              "/tinyhat_secrets list",
              "/tinyhat_secrets manage",
              "/tinyhat_secrets_manage",
              "/tinyhat_secrets add OPENAI_API_KEY",
            ],
          };
        }
        if (parsed.action === "list") {
          return fetchSecretStatuses(runtime.config, runtime.signal);
        }
        return callTinyhat(
          runtime.config,
          "/hapi/v1/computers/me/runtime-secrets/add-link",
          {
            method: "POST",
            body: JSON.stringify({
              name: parsed.name,
              hint: parsed.description,
              description: parsed.description,
            }),
          },
          runtime.signal,
        );
      },
    }),
  ],
});

const registerTools = plugin.register;

plugin.register = (api) => {
  registerTools(api);
  const platformConfig = api.pluginConfig ?? {};
  api.registerCommand({
    name: "tinyhat_secrets",
    nativeNames: { default: "tinyhat_secrets" },
    description: "List or request Tinyhat runtime secrets.",
    channels: ["telegram"],
    acceptsArgs: true,
    agentPromptGuidance: [
      "Use /tinyhat_secrets list to show runtime secret metadata and /tinyhat_secrets add NAME to request a Mini App secret-entry link. Never ask for secret values in chat.",
    ],
    handler: async (ctx) => {
      const parsed = parseSecretCommand(ctx.args || "");
      if (parsed.action === "help") {
        return {
          text: [
            "Tinyhat runtime secrets:",
            "/tinyhat_secrets list",
            "/tinyhat_secrets manage",
            "/tinyhat_secrets_manage",
            "/tinyhat_secrets add OPENAI_API_KEY why it is needed",
            "",
            "Secret values are added in the Tinyhat Mini App, not in chat.",
          ].join("\n"),
        };
      }
      if (parsed.action === "list") {
        const payload = await fetchSecretStatuses(platformConfig);
        return formatSecretListReply(payload);
      }
      const payload = await callTinyhat(
        platformConfig,
        "/hapi/v1/computers/me/runtime-secrets/add-link",
        {
          method: "POST",
          body: JSON.stringify({
            name: parsed.name,
            hint: parsed.description,
            description: parsed.description,
          }),
        },
      );
      return formatSecretRequestReply(payload);
    },
  });
  api.registerCommand({
    name: "tinyhat_secrets_manage",
    nativeNames: { default: "tinyhat_secrets_manage" },
    description: "Open Tinyhat runtime secret manager.",
    channels: ["telegram"],
    acceptsArgs: false,
    agentPromptGuidance: [
      "Use /tinyhat_secrets_manage when the user wants to open the Tinyhat runtime secret list Mini App.",
    ],
    handler: async () => {
      const payload = await fetchSecretStatuses(platformConfig);
      return formatSecretListReply(payload);
    },
  });
  api.registerCommand({
    name: "tinyhat_computer",
    nativeNames: { default: "tinyhat_computer" },
    description: "Open Tinyhat Manage Computer.",
    channels: ["telegram"],
    acceptsArgs: false,
    agentPromptGuidance: [
      "Use /tinyhat_computer when an agent admin asks to manage, inspect, or open controls for this Computer.",
    ],
    handler: async () => {
      const payload = await fetchManageComputerLink(platformConfig);
      return formatManageComputerReply(payload);
    },
  });
  api.registerCommand({
    name: "tinyhat_terminal",
    nativeNames: { default: "tinyhat_terminal" },
    description: "Open a secure Tinyhat terminal.",
    channels: ["telegram"],
    acceptsArgs: true,
    agentPromptGuidance: [
      "Use /tinyhat_terminal when an agent admin asks to open a secure terminal for this Computer. If a command follows the command name, the Mini App will show it for explicit admin approval before running it.",
    ],
    handler: async (ctx) => {
      const payload = await fetchTerminalLink(platformConfig, undefined, ctx.args);
      return formatTerminalReply(payload);
    },
  });
};

export default plugin;

function resolveExecutionRuntime(configArg, contextArg) {
  const configCandidate =
    firstPluginConfig(configArg) ?? firstPluginConfig(contextArg) ?? {};
  return {
    config: configCandidate,
    signal: contextArg?.signal ?? configArg?.signal,
  };
}

function firstPluginConfig(value) {
  if (!value || typeof value !== "object") {
    return null;
  }
  if (looksLikePlatformConfig(value)) {
    return value;
  }
  for (const key of ["pluginConfig", "config", "toolConfig"]) {
    const nested = value[key];
    if (nested && typeof nested === "object" && looksLikePlatformConfig(nested)) {
      return nested;
    }
  }
  return null;
}

function looksLikePlatformConfig(value) {
  if (!value || typeof value !== "object") {
    return false;
  }
  return [
    "platformBaseUrl",
    "backendAudience",
    "devMode",
    "devBearer",
  ].some((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function parseSecretCommand(raw) {
  const text = String(raw || "").trim();
  if (!text || /^(help|-h|--help)$/i.test(text)) {
    return { action: "help" };
  }
  const parts = text.split(/\s+/);
  const verb = (parts.shift() || "").toLowerCase();
  if (["list", "ls", "status", "show", "manage"].includes(verb)) {
    return { action: "list" };
  }
  if (["add", "set", "request", "need"].includes(verb)) {
    const name = parts.shift();
    if (!name) {
      return { action: "help" };
    }
    return { action: "request", name, description: parts.join(" ") || undefined };
  }
  if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(text)) {
    return { action: "request", name: text };
  }
  return { action: "help" };
}

function formatSecretListReply(payload) {
  const secrets = Array.isArray(payload?.secrets) ? payload.secrets : [];
  const button = payload?.telegram_button;
  if (secrets.length === 0) {
    return {
      text: "No Tinyhat runtime secrets are configured for this Computer yet.",
      ...(button?.web_app?.url ? secretManagePresentation(button) : {}),
    };
  }
  const lines = secrets.map((secret) => {
    const name = normalizeString(secret?.name) || "(unnamed)";
    const saved = secret?.in_platform || secret?.has_value ? "saved" : "not saved";
    const available =
      normalizeString(secret?.vps_status) === "available"
        ? "available"
        : "not available";
    const description = normalizeString(secret?.description);
    return `- ${name}: ${saved}, ${available}${description ? ` — ${description}` : ""}`;
  });
  return {
    text: ["Tinyhat runtime secrets:", ...lines].join("\n"),
    ...(button?.web_app?.url ? secretManagePresentation(button) : {}),
  };
}

function formatSecretRequestReply(payload) {
  const secretName = normalizeString(payload?.secret?.name) || "this secret";
  const button = payload?.telegram_button;
  const url = normalizeString(payload?.mini_app_url);
  const text = [
    `Open the Tinyhat Mini App to add ${secretName}.`,
    "Do not paste the value in chat.",
    url,
  ]
    .filter(Boolean)
    .join("\n");
  if (!button?.web_app?.url) {
    return { text };
  }
  return {
    text,
    channelData: { telegram: { buttons: [[button]] } },
    presentation: {
      blocks: [
        {
          type: "buttons",
          buttons: [
            {
              label: normalizeString(button.text) || `Add ${secretName}`,
              webApp: { url: button.web_app.url },
            },
          ],
        },
      ],
    },
  };
}

function secretManagePresentation(button) {
  return {
    channelData: { telegram: { buttons: [[button]] } },
    presentation: {
      blocks: [
        {
          type: "buttons",
          buttons: [
            {
              label: normalizeString(button.text) || "Manage secrets",
              webApp: { url: button.web_app.url },
            },
          ],
        },
      ],
    },
  };
}

function formatManageComputerReply(payload) {
  const button = payload?.telegram_button;
  const message = normalizeString(payload?.message);
  if (!button?.web_app?.url) {
    return {
      text: message || "Tinyhat Manage Computer is not available.",
    };
  }
  const text =
    message ||
    "Open a secure management page for this Computer. Only admins of this agent can open it.";
  return {
    text,
    channelData: { telegram: { buttons: [[button]] } },
    presentation: {
      blocks: [
        {
          type: "buttons",
          buttons: [
            {
              label: normalizeString(button.text) || "Manage computer",
              webApp: { url: button.web_app.url },
            },
          ],
        },
      ],
    },
  };
}

function formatTerminalReply(payload) {
  const button = payload?.telegram_button;
  const message = normalizeString(payload?.message);
  if (!button?.web_app?.url) {
    return {
      text: message || "Tinyhat terminal is not available for this Computer.",
    };
  }
  const text =
    message ||
    "Open a secure terminal to this computer and run commands as an admin. Only admins of this agent can open it.";
  return {
    text,
    channelData: { telegram: { buttons: [[button]] } },
    presentation: {
      blocks: [
        {
          type: "buttons",
          buttons: [
            {
              label: normalizeString(button.text) || "Open secure terminal",
              webApp: { url: button.web_app.url },
            },
          ],
        },
      ],
    },
  };
}

async function fetchManageComputerLink(config, signal) {
  return callTinyhat(
    config,
    "/hapi/v1/computers/me/manage/open-link",
    { method: "POST" },
    signal,
  );
}

async function fetchSecretStatuses(config, signal) {
  return callTinyhat(
    config,
    "/hapi/v1/computers/me/runtime-secret-statuses",
    { method: "GET" },
    signal,
  );
}

async function fetchTerminalLink(config, signal, command) {
  const normalizedCommand = normalizeString(command);
  const init = normalizedCommand
    ? { method: "POST", body: JSON.stringify({ command: normalizedCommand }) }
    : { method: "POST" };
  return callTinyhat(
    config,
    "/hapi/v1/computers/me/terminal/open-link",
    init,
    signal,
  );
}

async function callTinyhat(config, path, init, signal) {
  const baseUrl = await resolvePlatformBaseUrl(config, signal);
  const token = await resolveBearerToken(config, signal);
  const response = await fetch(baseUrl.replace(/\/+$/, "") + path, {
    ...init,
    signal,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...(init.headers || {}),
    },
  });
  const text = await response.text();
  const payload = text ? parseJson(text) : {};
  if (!response.ok) {
    throw new Error(
      `Tinyhat request failed (${response.status}): ${readDetail(payload, text)}`,
    );
  }
  return payload;
}

async function resolvePlatformBaseUrl(config, signal) {
  const configured = normalizeString(config?.platformBaseUrl);
  if (configured) {
    return configured;
  }
  const devConfigured = readProcessEnv("TINYHAT_PLATFORM_BASE_URL");
  if (devConfigured) {
    return devConfigured;
  }
  return readMetadataValue(METADATA_BASE_URL_KEY, signal);
}

async function resolveBearerToken(config, signal) {
  const isDev =
    config?.devMode === true ||
    readProcessEnv("TINYHAT_DEV_RUNTIME") === "1";
  if (isDev) {
    return (
      normalizeString(config?.devBearer) ||
      readProcessEnv("TINYHAT_DEV_BEARER") ||
      DEV_RUNTIME_BEARER
    );
  }
  const audience =
    normalizeString(config?.backendAudience) ||
    (await readMetadataValue(METADATA_AUDIENCE_KEY, signal));
  const url =
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/" +
    `default/identity?audience=${encodeURIComponent(audience)}&format=full`;
  const response = await fetch(url, {
    signal,
    headers: { "Metadata-Flavor": "Google" },
  });
  if (!response.ok) {
    throw new Error(`Could not fetch Tinyhat Computer identity token (${response.status}).`);
  }
  return (await response.text()).trim();
}

async function readMetadataValue(key, signal) {
  const url =
    "http://metadata.google.internal/computeMetadata/v1/instance/attributes/" +
    encodeURIComponent(key);
  const response = await fetch(url, {
    signal,
    headers: { "Metadata-Flavor": "Google" },
  });
  if (!response.ok) {
    throw new Error(`Could not read metadata ${key} (${response.status}).`);
  }
  return (await response.text()).trim();
}

function normalizeString(value) {
  return String(value || "").trim();
}

function readProcessEnv(name) {
  if (typeof process === "undefined" || !process.env) {
    return "";
  }
  return normalizeString(process.env[name]);
}

function parseJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
}

function readDetail(payload, fallback) {
  if (payload && typeof payload.detail === "string") {
    return payload.detail;
  }
  return String(fallback || "").slice(0, 240) || "unknown error";
}
