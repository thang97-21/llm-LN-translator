window.__ModuleLoader__.load({
	id: "dsh-client-mtls",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		let react = require("react");

		// One style tag for the panel, injected once per page.
		const tagId = "dsh-client-mtls/panel.css";
		if (typeof document !== "undefined" && document.querySelector('style[data-plugin-css="' + tagId + '"]') === null) {
			const tag = document.createElement("style");
			tag.dataset.plugin = "dsh-client-mtls";
			tag.dataset.pluginCss = tagId;
			tag.textContent =
				".mtls-panel-pill{" +
				"min-height:28px;border:1px solid var(--dsw-alias-border-l2);" +
				"background:var(--dsw-alias-fill-l2);color:var(--dsw-alias-label-primary);" +
				"border-radius:999px;padding:3px 10px;font-size:12px;line-height:18px;" +
				"cursor:pointer;display:inline-flex;align-items:center;gap:4px}" +
				".mtls-panel-pill:hover{border-color:var(--dsw-alias-border-l3)}" +
				".mtls-panel{position:absolute;right:0;top:calc(100% + 6px);z-index:50;" +
				"min-width:320px;max-width:420px;border:1px solid var(--dsw-alias-border-l2);" +
				"border-radius:8px;background:var(--dsw-alias-fill-l1);" +
				"color:var(--dsw-alias-label-primary);font-size:12px;line-height:18px;" +
				"box-shadow:0 8px 24px rgba(0,0,0,.18);padding:10px 12px}" +
				".mtls-panel h4{margin:0 0 6px;font-size:12px;font-weight:600}" +
				".mtls-panel ol{margin:0 0 8px;padding-left:18px}" +
				".mtls-panel .mtls-skill{cursor:pointer;text-decoration:underline dotted;color:var(--dsw-alias-label-secondary)}" +
				".mtls-panel .mtls-gate{border-top:1px solid var(--dsw-alias-border-l2);margin-top:6px;padding-top:6px;color:var(--dsw-alias-label-secondary)}";
			document.head.appendChild(tag);
		}

		/**
		 * MTLS pipeline map — static doctrine mirror (Phase 6 increment 1).
		 * Live manifest.json data arrives in increment 2 behind a host
		 * projection; this panel renders the pipeline topology, the QC gate
		 * law, and the dispatch skills, with click-to-copy invocation.
		 */
		const PIPELINE = [
			{ phase: "extract", skill: "deepseek-translator" },
			{ phase: "prep", skill: "mtls-prep-dispatch" },
			{ phase: "translate", skill: "mtls-pipeline-dispatch" },
			{ phase: "qc", skill: "mtls-qc-dispatch" },
			{ phase: "build", skill: "mtls-pipeline-dispatch" }
		];
		const GATE_LAW =
			"Never skip the QC gate. blocked = STOP. Audit mandatory for every outcome. Sub-agent JSON files are ground truth.";

		/** Copy a skill name to the clipboard; no backend involved. */
		function copySkill(name) {
			try {
				if (navigator.clipboard && navigator.clipboard.writeText) {
					navigator.clipboard.writeText(name);
				} else {
					const ta = document.createElement("textarea");
					ta.value = name;
					document.body.appendChild(ta);
					ta.select();
					document.execCommand("copy");
					document.body.removeChild(ta);
				}
			} catch (_) { /* clipboard unavailable — the pill still opens */ }
		}

		/**
		 * Session-header control: a pill that toggles the MTLS pipeline panel.
		 * The header-actions owner passes nothing; this control needs nothing
		 * but React, so it stays self-sufficient.
		 */
		function MtlsPanel() {
			const [open, setOpen] = react.useState(false);
			return react.createElement(
				"div",
				{ style: { position: "relative", display: "inline-flex" } },
				react.createElement(
					"button",
					{
						type: "button",
						className: "mtls-panel-pill",
						title: "MTLS pipeline workspace — phase map, gate law, dispatch skills.",
						onClick: () => setOpen((v) => !v)
					},
					open ? "MTLS Pipeline ▾" : "MTLS Pipeline ▸"
				),
				open ? react.createElement(
					"div",
					{ className: "mtls-panel" },
					react.createElement("h4", null, "MTLS Pipeline — work/<vol_id>"),
					react.createElement(
						"ol",
						null,
						PIPELINE.map((p) => react.createElement(
							"li",
							{ key: p.phase },
							p.phase,
							" → ",
							react.createElement(
								"span",
								{
									className: "mtls-skill",
									title: "Click to copy skill name",
									onClick: () => copySkill(p.skill)
								},
								p.skill
							)
						))
					),
					react.createElement("div", { className: "mtls-gate" }, GATE_LAW)
				) : null
			);
		}

		/** Required services for this plugin's apply. */
		const inject = ["slots"];

		/**
		 * Client plugin body: contribute one entry to the session header's
		 * action row. Registration disposal rides this fiber's ctx, so an
		 * unload of the plugin takes the entry with it.
		 * @param ctx - client root context.
		 */
		function apply(ctx) {
			ctx.slots.inject("conversation.session.header.actions", () => ctx.slots.register({
				name: "conversation.session.header.actions",
				id: "mtls-pipeline",
				order: 100,
				label: "MTLS Pipeline"
			}, MtlsPanel));
		}

		exports.apply = apply;
		exports.inject = inject;
		return module.exports;
	}
});
