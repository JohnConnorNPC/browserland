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
            // legacy StreamLanguage modes (StreamLanguage itself comes from our
            // single @codemirror/language instance above; these submodules are
            // pure StreamParser data, so they add NO second @codemirror/state
            // importer). ?target=es2022 matches the core imports' build target so
            // esm.sh dedupes any shared dep onto the same concrete URL (#36).
            lmShell: '@codemirror/legacy-modes@6.5.1/mode/shell?target=es2022',
            lmToml: '@codemirror/legacy-modes@6.5.1/mode/toml?target=es2022',
            lmDocker: '@codemirror/legacy-modes@6.5.1/mode/dockerfile?target=es2022',
            lmProps: '@codemirror/legacy-modes@6.5.1/mode/properties?target=es2022',
        };
        let _cmModulesPromise = null;
        function loadCodeMirror() {
            if (_cmModulesPromise) return _cmModulesPromise;
            // Name-keyed resolve: import every CM_VER entry in key order and zip
            // the results back onto a `m` map by the SAME keys, so adding/removing
            // an import can never off-by-one a positional destructure (codex).
            const keys = Object.keys(CM_VER);
            _cmModulesPromise = Promise.all(keys.map((k) => import(CM_CDN + CM_VER[k])))
                .then((arr) => {
                    const m = {};
                    keys.forEach((k, i) => { m[k] = arr[i]; });
                    // StreamLanguage rides our single @codemirror/language
                    // instance, so the legacy modes share its state facets (#36).
                    const SL = m.language.StreamLanguage;
                    return {
                        view: m.view, state: m.state, language: m.language,
                        commands: m.commands, search: m.search,
                        autocomplete: m.autocomplete, theme: m.theme,
                        // language-pack factories keyed by the names detectLanguage emits
                        langs: {
                            javascript: () => m.js.javascript(),
                            jsx: () => m.js.javascript({ jsx: true }),
                            typescript: () => m.js.javascript({ typescript: true }),
                            tsx: () => m.js.javascript({ typescript: true, jsx: true }),
                            python: () => m.py.python(),
                            json: () => m.json.json(),
                            html: () => m.html.html(),
                            css: () => m.css.css(),
                            markdown: () => m.md.markdown(),
                            cpp: () => m.cpp.cpp(),
                            rust: () => m.rust.rust(),
                            go: () => m.go.go(),
                            sql: () => m.sql.sql(),
                            xml: () => m.xml.xml(),
                            yaml: () => m.yaml.yaml(),
                            java: () => m.java.java(),
                            // StreamLanguage modes (legacy-modes export names:
                            // shell.shell, toml.toml, dockerfile.dockerFile,
                            // properties.properties — note the capital F).
                            shell: () => SL.define(m.lmShell.shell),
                            toml: () => SL.define(m.lmToml.toml),
                            dockerfile: () => SL.define(m.lmDocker.dockerFile),
                            properties: () => SL.define(m.lmProps.properties),
                        },
                    };
                }).catch((e) => {
                    // Allow a later retry (e.g. transient CDN blip) by clearing the
                    // cache, then re-reject so this caller keeps the textarea.
                    _cmModulesPromise = null;
                    throw e;
                });
            return _cmModulesPromise;
        }
        // Resolve a file path to a detectLanguage key (see langs above). Order:
        // extension map -> basename map (extensionless/dotfiles like Dockerfile,
        // .bashrc) -> prefix rules (Dockerfile.dev, .env.local) -> content sniff
        // (shebang/markup) -> null (plain, no highlighting). `log`/`txt`, and
        // Makefile/.gitignore/.dockerignore are INTENTIONALLY unmapped (no good
        // mode) -> they fall through to null and open as clean plain text.
        function detectLanguage(filePath, sample) {
            const p = String(filePath || '');
            // Basename: drop any trailing separators, then take the last segment
            // (split on EITHER separator so C:\a\b and /a/b both work).
            const trimmed = p.replace(/[\/\\]+$/, '');
            const cut = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'));
            const base = cut === -1 ? trimmed : trimmed.slice(cut + 1);
            const lower = base.toLowerCase();
            // Extension = text after the LAST dot, but a LEADING dot is "no ext"
            // (dot>0), so `.bashrc` has none while `.eslintrc.json` keeps `json`.
            const dot = base.lastIndexOf('.');
            const ext = dot > 0 ? base.slice(dot + 1).toLowerCase() : '';
            const byExt = {
                js: 'javascript', mjs: 'javascript', cjs: 'javascript',
                jsx: 'jsx',
                ts: 'typescript', mts: 'typescript', cts: 'typescript', tsx: 'tsx',
                py: 'python', pyw: 'python', pyi: 'python',
                json: 'json', jsonc: 'json', json5: 'json',
                html: 'html', htm: 'html', xhtml: 'html',
                css: 'css',
                md: 'markdown', markdown: 'markdown', mdown: 'markdown', mkd: 'markdown',
                xml: 'xml', svg: 'xml', xsd: 'xml', xsl: 'xml', xslt: 'xml',
                plist: 'xml', rss: 'xml', atom: 'xml',
                yaml: 'yaml', yml: 'yaml',
                sh: 'shell', bash: 'shell', zsh: 'shell', ksh: 'shell',
                toml: 'toml',
                ini: 'properties', conf: 'properties', cfg: 'properties',
                properties: 'properties',
                rs: 'rust', go: 'go',
                c: 'cpp', h: 'cpp', cpp: 'cpp', cc: 'cpp', cxx: 'cpp',
                hpp: 'cpp', hh: 'cpp',
                java: 'java',
                sql: 'sql',
            };
            if (ext && byExt[ext]) return byExt[ext];
            // Extensionless / dotfile basenames (lowercased — case-insensitive by
            // intent, so Dockerfile and DOCKERFILE both match). Makefile,
            // .gitignore, .dockerignore are deliberately absent -> plain text.
            const byName = {
                dockerfile: 'dockerfile', containerfile: 'dockerfile',
                '.bashrc': 'shell', '.bash_profile': 'shell', '.zshrc': 'shell',
                '.profile': 'shell', '.kshrc': 'shell',
                '.editorconfig': 'properties', '.npmrc': 'properties',
                '.inputrc': 'properties',
            };
            if (byName[lower]) return byName[lower];
            // Prefix rules: Dockerfile.dev/Dockerfile.prod, and .env/.env.local.
            if (lower.startsWith('dockerfile.')) return 'dockerfile';
            if (lower === '.env' || lower.startsWith('.env.')) return 'properties';
            // Content sniff for files no name/extension matched (nice-to-have).
            const s = String(sample || '').slice(0, 200);
            if (/^#!.*\b(sh|bash|zsh|ksh)\b/.test(s)) return 'shell';
            if (/^#!.*\bpython/.test(s)) return 'python';
            if (/^#!.*\bnode/.test(s)) return 'javascript';
            if (/^\s*<\?xml/.test(s)) return 'xml';
            if (/^\s*<!DOCTYPE html/i.test(s)) return 'html';
            return null;
        }

