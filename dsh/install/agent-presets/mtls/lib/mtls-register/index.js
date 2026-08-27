// MTLS Register — dynamic persona provider for the `mtls` preset.
//
// The merged MTLS persona (Standard Mirei + MTLS pipeline doctrine) is read
// from the workspace at every assembly — editing the authored file takes
// effect on the next turn, no restart. The text adapts to the agent:
//
//  - the principal (chat) agent keeps the full register, plus the desk line;
//  - a delegated subagent (session header carries `parentSession`) gets a
//    neutral executor statement — the register is principal-only.
//
// Path resolution, in order: row `config.personaPath` (if the loader forwards
// it), env `MTLS_PERSONA_PATH`, then the workspace default. This package stays
// dependency-free so its Node resolution never leaves the preset directory.
//
// This row lives in the preset composition, so `deployment:persona` is
// registered in the standing agent scope: it shadows the global static
// persona for this preset's agents and joins every session by scope
// parentage.

import { readFileSync } from "node:fs";

/** Cordis plugin name. */
const name = "mtls-register";
/** The prompt registry this row contributes to. */
const inject = ["systemPrompt"];
/** Section identity: shadows the global static persona in this scope. */
const PERSONA_SECTION = "deployment:persona";
/** Prompt order of the persona slot; the first section a model reads. */
const PERSONA_ORDER = 0;
/** Default register path for this desk. */
const DEFAULT_PERSONA = "D:/MTLS/.dsh/persona/mirei-mtls.md";

/** Neutral executor statement for delegated subagents. */
const NEUTRAL_SUBAGENT = [
	"You are a delegated subagent acting for the MTLS principal.",
	"Work precisely, cite exact paths and evidence, and report plainly.",
	"The principal's persona does not extend to you."
].join("\n");

/** The authored register, read fresh at each assembly. */
function readRegister(ctx, path) {
	try {
		return readFileSync(path, "utf8");
	} catch (error) {
		ctx.logger.warn(`mtls-register: cannot read ${path} — ${error.message}`);
		return "";
	}
}

/**
 * Session facts the persona can sense from the assembly context. Only fields
 * verified on the session header are read; anything absent degrades to "".
 */
function sessionFacts(context) {
	const header = context?.agent?.session?.header;
	return {
		workspace: typeof header?.cwd === "string" && header.cwd.length > 0 ? header.cwd : "",
		delegated: header?.parentSession !== undefined
	};
}

/**
 * Register the dynamic persona section and the session variables it senses.
 * @param ctx - the preset-scope context this row mounts in.
 * @param config - optional row config; `personaPath` wins over env/default.
 */
function apply(ctx, config = {}) {
	const personaPath = config?.personaPath
		?? process.env.MTLS_PERSONA_PATH
		?? DEFAULT_PERSONA;

	ctx.systemPrompt.variable("mtls_workspace", (context) => sessionFacts(context).workspace);
	ctx.systemPrompt.variable("mtls_delegated", (context) => sessionFacts(context).delegated ? "yes" : "");

	// Live desk snapshot: rendered into the runtime-context block every turn,
	// just before the harness's own policy facts (sandbox at 110, approval at
	// 115), so the pipeline state is the first thing the snapshot reports.
	ctx.effect(() => ctx.systemPrompt.context({
		name: "mtls:desk",
		order: 105,
		text: (context) => {
			const facts = sessionFacts(context);
			const options = context?.agent?.options;
			const engine = [options?.provider, options?.model].filter((value) => typeof value === "string" && value.length > 0).join("/");
			const lines = [];
			if (facts.workspace.length > 0) lines.push(`Desk: ${facts.workspace}`);
			if (engine.length > 0) lines.push(`Engine: ${engine}`);
			lines.push(`Role: ${facts.delegated ? "delegated subagent" : "principal"}`);
			return lines.join("\n");
		}
	}), "mtls-register.desk.context()");

	ctx.effect(() => ctx.systemPrompt.section({
		name: PERSONA_SECTION,
		order: PERSONA_ORDER,
		text: (context) => {
			const facts = sessionFacts(context);
			if (facts.delegated) return NEUTRAL_SUBAGENT;
			const base = readRegister(ctx, personaPath);
			if (base.length === 0) return "";
			if (facts.workspace.length === 0) return base;
			return `${base}\n\nThe desk: ${facts.workspace} — the commission on it is hers to manage.`;
		}
	}), "mtls-register.persona.section()");
}

export { apply, inject, name };
