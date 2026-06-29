        // ---- CodeMirror 6 lazy loader (text-editor syntax highlighting) ----
        // Loaded on first editor open from an ESM CDN. Cached in a module-level
        // promise so multiple editors share one load. On ANY import failure we
        // reject; the caller's .catch keeps the plain <textarea> editor
        // (mandatory offline fallback). Language packs are loaded eagerly
        // alongside the core — they're small and detection needs whichever one a
        // file uses.
        //
        // CDN choice — esm.sh, NOT jsdelivr/+esm: CodeMirror 6 is split into
        // many npm packages that all share a SINGLE @codemirror/state module,
        // and its facets rely on that module being one instance. jsdelivr's
        // /+esm bakes a different pinned @codemirror/state version into each
        // package's bundle (view->6.5.0, lang-javascript->6.4.1, ...), yielding
        // several state instances whose facet identities don't match — which
        // silently breaks language/extension wiring.
        //
        // esm.sh dedupes a shared dep by its RESOLVED concrete-build URL, but
        // ONLY when every importer lands on that same build. view/language/
        // autocomplete/commands import @codemirror/state via semver RANGES
        // (^6.5.0, ^6.4.0, ...) + the same ?target=es2022 the browser uses, and
        // esm.sh resolves all of them to the LATEST matching build's es2022 URL
        // (currently /@codemirror/state@6.7.0/es2022/state.mjs).
        //
        // So `state` below is imported via the SAME request the transitive
        // importers make — `@codemirror/state@^6.5.0?target=es2022` (verbatim
        // what view@6.36.2's build imports) — NOT a manually pinned version.
        // Two things make this the durable form:
        //   - the RANGE auto-tracks whatever esm.sh resolves the transitive
        //     ranges to, so it can't drift behind them (a low exact pin loaded a
        //     2nd state instance -> CM throws "Unrecognized extension value ...
        //     multiple instances of @codemirror/state" -> textarea fallback, #36);
        //   - the explicit ?target=es2022 pins the build target so the direct
        //     import can't land on a different per-UA build than the transitive
        //     ?target=es2022 imports (e.g. under a non-es2022 browser default).
        // DO NOT replace this with a bare exact version — that reintroduces #36
        // the next time upstream advances the range resolution. (xterm needs none
        // of this — single self-contained package.)
        const CM_CDN = 'https://esm.sh/';
        const CM_VER = {
            view: '@codemirror/view@6.36.2',
            state: '@codemirror/state@^6.5.0?target=es2022',
            language: '@codemirror/language@6.10.8',
            commands: '@codemirror/commands@6.8.0',
            search: '@codemirror/search@6.5.10',
            autocomplete: '@codemirror/autocomplete@6.18.6',
            theme: '@codemirror/theme-one-dark@6.1.2',
            js: '@codemirror/lang-javascript@6.2.2',
            py: '@codemirror/lang-python@6.1.7',
            json: '@codemirror/lang-json@6.0.1',
            html: '@codemirror/lang-html@6.4.9',
            css: '@codemirror/lang-css@6.3.1',
            md: '@codemirror/lang-markdown@6.3.2',
            cpp: '@codemirror/lang-cpp@6.0.2',
            rust: '@codemirror/lang-rust@6.0.1',
            go: '@codemirror/lang-go@6.0.1',
            sql: '@codemirror/lang-sql@6.8.0',
            xml: '@codemirror/lang-xml@6.1.0',
            yaml: '@codemirror/lang-yaml@6.1.2',
            java: '@codemirror/lang-java@6.0.1',
        };
        let _cmModulesPromise = null;
        function loadCodeMirror() {
            if (_cmModulesPromise) return _cmModulesPromise;
            const imp = (spec) => import(CM_CDN + CM_VER[spec]);
            _cmModulesPromise = Promise.all([
                imp('view'), imp('state'), imp('language'), imp('commands'),
                imp('search'), imp('autocomplete'), imp('theme'),
                imp('js'), imp('py'), imp('json'), imp('html'), imp('css'),
                imp('md'), imp('cpp'), imp('rust'), imp('go'), imp('sql'),
                imp('xml'), imp('yaml'), imp('java'),
            ]).then(([
                view, state, language, commands, search, autocomplete, theme,
                js, py, json, html, css, md, cpp, rust, go, sql, xml, yaml, java,
            ]) => ({
                view, state, language, commands, search, autocomplete, theme,
                // language-pack factories keyed by the names detectLanguage emits
                langs: {
                    javascript: () => js.javascript(),
                    jsx: () => js.javascript({ jsx: true }),
                    typescript: () => js.javascript({ typescript: true }),
                    tsx: () => js.javascript({ typescript: true, jsx: true }),
                    python: () => py.python(),
                    json: () => json.json(),
                    html: () => html.html(),
                    css: () => css.css(),
                    markdown: () => md.markdown(),
                    cpp: () => cpp.cpp(),
                    rust: () => rust.rust(),
                    go: () => go.go(),
                    sql: () => sql.sql(),
                    xml: () => xml.xml(),
                    yaml: () => yaml.yaml(),
                    java: () => java.java(),
                },
            })).catch((e) => {
                // Allow a later retry (e.g. transient CDN blip) by clearing the
                // cache, then re-reject so this caller keeps the textarea.
                _cmModulesPromise = null;
                throw e;
            });
            return _cmModulesPromise;
        }
        // Map a file path's extension to a detectLanguage key (see langs above).
        // Unknown/blank -> null (plain, no highlighting). A tiny content sniff
        // covers extension-less shebang/markup files.
        function detectLanguage(filePath, sample) {
            const p = String(filePath || '');
            const dot = p.lastIndexOf('.');
            const slash = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
            const ext = (dot > slash && dot !== -1) ? p.slice(dot + 1).toLowerCase() : '';
            const byExt = {
                js: 'javascript', mjs: 'javascript', cjs: 'javascript',
                jsx: 'jsx', ts: 'typescript', tsx: 'tsx',
                py: 'python', pyw: 'python',
                json: 'json', jsonc: 'json',
                html: 'html', htm: 'html',
                css: 'css',
                md: 'markdown', markdown: 'markdown',
                xml: 'xml', svg: 'xml',
                yaml: 'yaml', yml: 'yaml',
                sh: 'cpp', bash: 'cpp',           // no shell pack; cpp ~ ok-ish
                rs: 'rust', go: 'go',
                c: 'cpp', h: 'cpp', cpp: 'cpp', cc: 'cpp', cxx: 'cpp',
                hpp: 'cpp', hh: 'cpp',
                java: 'java',
                sql: 'sql',
            };
            if (byExt[ext]) return byExt[ext];
            // Content sniff for extension-less files (nice-to-have).
            const s = String(sample || '').slice(0, 200);
            if (/^#!.*\b(sh|bash)\b/.test(s)) return 'cpp';
            if (/^#!.*\bpython/.test(s)) return 'python';
            if (/^#!.*\bnode/.test(s)) return 'javascript';
            if (/^\s*<\?xml/.test(s)) return 'xml';
            if (/^\s*<!DOCTYPE html/i.test(s)) return 'html';
            return null;
        }

