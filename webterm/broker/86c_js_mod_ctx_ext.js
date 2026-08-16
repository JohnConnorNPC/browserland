        // ---- ctx extensions (#194) ------------------------------------------
        // Where NEW per-mod ctx surface lands. 86_js_mod_loader.js is at the
        // #68 2500-line per-fragment cap (_MAX_LINES, ui.py), and the rule for
        // that cap has always been "split, never trim" — 86a (#168) and 86b
        // (#163) are the precedent. So the loader keeps ctx v1 and the
        // EXTENDER REGISTRY; every family added after it is declared here (or
        // in a later 86*-ordered fragment) and registered into that registry.
        //
        // How to add one:
        //
        //     // ---- ctx.<family> (#<issue>) ----
        //     function _ctx<Family>(ctx, rec) {
        //         ctx.<family> = { … };          // decorate in place
        //     }
        //     _registerCtxExtender(_ctx<Family>);
        //
        // The three rules that make that safe, all enforced by the registry in
        // 86_js_mod_loader.js (see the "ctx-extender registry" block there):
        //
        //  1. ARGUMENTS, NOT CLOSURE. Assembly concatenates every fragment into
        //     ONE <script>, so a top-level function declared here is callable
        //     from the loader and vice versa — but this fragment can NOT see
        //     makeCtx's per-mod locals (modId, ns, …). An extender is handed
        //     `ctx` (whose `id` is the mod id) and `rec` (the active-mod record:
        //     `rec.unloads` is the LIFO teardown list every ctx family already
        //     registers its disposers on), and anything else it needs has to
        //     arrive as an argument too. Same discipline as
        //     mods/update/update-apply.js.
        //  2. ISOLATION AND ORDER. Extenders run in registration order, which is
        //     ui._ORDERED order; a throwing one is logged and skipped without
        //     taking its siblings — or the mod's ctx — down. So a surface that
        //     fails to install is one missing family a mod can feature-detect,
        //     never a dead desktop.
        //  3. ADDITIVE, FEATURE-DETECTED. `ctxVersion` stays 1 (declared in the
        //     loader, enforced in initMod): every family here is additive, and a
        //     mod tests for it the way it tests for ctx.file —
        //     `if (ctx.<family>)` / `typeof ctx.<family>.<fn> === 'function'`.
        //     Bumping the version would refuse every mod that pins v1.
        //
        // Nothing runs at load beyond the _registerCtxExtender calls themselves:
        // an extender body executes once per mod init, from makeCtx.
        //
        // No extender is registered yet — #194's ctx.windows.createAppWindow is
        // the first, and #195-#198 ride the same registry and this same
        // fragment. The fragment ships now so the seam (and its ui._ORDERED
        // slot) exists before the surfaces that need it, which is the ordering
        // this repo already uses for a consumer that precedes its producer.
