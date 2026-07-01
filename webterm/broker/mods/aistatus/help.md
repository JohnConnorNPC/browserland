The AI status chip shows the worst-case health of the major AI providers at a glance, so when a `/loop` or an MCP call starts failing you can tell whether it's you or the provider. It appears whenever the AI status mod is enabled — there is no separate toggle. Turn it on or off from Control Panel → Mods.

The mod ships **disabled by default**. Enabling it lets the broker fetch each provider's public status page (Anthropic, OpenAI, Cohere, and GitHub Copilot) through its allowlisted `/status/fetch` proxy — which makes the broker's egress IP visible to those status hosts — so it stays off until you opt in. The broker only ever reaches that fixed allowlist of provider status pages; it never follows a caller-supplied URL.

The chip aggregates the worst indicator across the providers you have enabled: green when all are operational, amber for a minor or major issue, red for a critical outage, and grey when a provider can't be reached. Click it to open the **AI status** window, which lists each provider with a colored dot, its current status, and any active incidents, plus a **Refresh** button and a last-checked time.

The mod's Control Panel → Mods section has a checkbox per provider (so you can monitor only the ones you use) and a poll-interval selector (30 seconds to 5 minutes). Those settings are browser-global, shared across every host you drive from this browser.
