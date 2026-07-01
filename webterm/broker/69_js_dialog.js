        // ---- reusable styled dialog (#72, Part A) -------------------------
        // One in-app modal primitive + thin wrappers, all hoisted globals so
        // core AND mods can call them (the same posture as openFileDialog, which
        // this mirrors). Built DYNAMICALLY: openDialog creates an
        // `.app-dialog-overlay > .app-dialog`, appends it to <body>, and removes
        // it on finish — no static markup. A module-level singleton guard
        // (_dlgFinish) cancels any live dialog before opening a new one, so a
        // second call can't orphan the first promise + its capture-phase Escape
        // listener. The styling reuses the shared dialog CSS groups
        // (15_css_dialogs.css), so these match the Control Panel / file dialog
        // look. z-index 185000 sits above #file-overlay (180000) and below
        // #ctx-menu (200000) / #auth-overlay (250000).
        //
        // openDialog(spec) -> Promise<{value,fields}|null>
        //   spec = { title, body?(container), fields?:[{key,label,value,
        //            placeholder,validate}], buttons?:[{label,value,primary,
        //            danger}], initialFocus }
        //   Resolves to {value:<clicked button.value>, fields:{<key>:<string>}}
        //   on a button click, or null on Escape / backdrop click. Primary-button
        //   commits run every field's validate(value) first: a truthy return is
        //   shown as the error and the dialog stays open.
        let _dlgFinish = null;
        function openDialog(spec) {
            if (_dlgFinish) _dlgFinish(null);   // cancel any live dialog first
            spec = spec || {};
            const overlay = document.createElement('div');
            overlay.className = 'app-dialog-overlay';
            const modal = document.createElement('div');
            modal.className = 'app-dialog';
            overlay.appendChild(modal);

            if (spec.title) {
                const head = document.createElement('div');
                head.className = 'set-head';
                head.textContent = spec.title;
                modal.appendChild(head);
            }
            // Optional caller-built body (Properties rows, confirm message, …).
            if (typeof spec.body === 'function') {
                const bodyWrap = document.createElement('div');
                bodyWrap.className = 'app-dialog-body';
                try { spec.body(bodyWrap); } catch (_) {}
                modal.appendChild(bodyWrap);
            }
            // Text fields (Rename / New folder / Zip name).
            const fieldInputs = [];
            if (Array.isArray(spec.fields)) {
                for (const f of spec.fields) {
                    const row = document.createElement('div');
                    row.className = 'set-row app-dialog-field';
                    if (f.label) {
                        const lab = document.createElement('label');
                        lab.textContent = f.label;
                        row.appendChild(lab);
                    }
                    const input = document.createElement('input');
                    input.type = 'text';
                    input.value = (f.value != null) ? String(f.value) : '';
                    if (f.placeholder) input.placeholder = f.placeholder;
                    row.appendChild(input);
                    modal.appendChild(row);
                    fieldInputs.push({ key: f.key, input: input,
                                       validate: f.validate });
                }
            }
            const errEl = document.createElement('div');
            errEl.className = 'set-err';
            modal.appendChild(errEl);

            const buttons = (Array.isArray(spec.buttons) && spec.buttons.length)
                ? spec.buttons
                : [{ label: 'OK', value: true, primary: true }];
            const foot = document.createElement('div');
            foot.className = 'set-foot app-dialog-foot';
            modal.appendChild(foot);

            return new Promise(function (resolve) {
                let primarySpec = null;
                const showErr = function (m) {
                    errEl.textContent = m; errEl.classList.add('show');
                };
                const collectFields = function (validate) {
                    const out = {};
                    for (const fi of fieldInputs) {
                        const val = fi.input.value;
                        if (validate && typeof fi.validate === 'function') {
                            const msg = fi.validate(val);
                            if (msg) { showErr(msg); fi.input.focus(); return null; }
                        }
                        out[fi.key] = val;
                    }
                    return out;
                };
                const finish = function (val) {
                    if (_dlgFinish === finish) _dlgFinish = null;
                    document.removeEventListener('keydown', onKey, true);
                    if (overlay.parentNode) {
                        overlay.parentNode.removeChild(overlay);
                    }
                    resolve(val);
                };
                _dlgFinish = finish;
                const choose = function (btn) {
                    // Validate only on a primary commit; a cancel/secondary
                    // button never blocks on a bad field.
                    const fields = collectFields(!!btn.primary);
                    if (fields === null) return;        // validation failed
                    finish({ value: btn.value, fields: fields });
                };
                for (const btn of buttons) {
                    const b = document.createElement('button');
                    b.type = 'button';
                    b.textContent = btn.label || 'OK';
                    if (btn.primary) b.classList.add('primary');
                    if (btn.danger) b.classList.add('danger');
                    b.addEventListener('click', function (e) {
                        e.stopPropagation(); choose(btn);
                    });
                    foot.appendChild(b);
                    if (btn.primary && !primarySpec) primarySpec = btn;
                }
                const onKey = function (e) {
                    if (e.key === 'Escape') {
                        e.preventDefault(); e.stopPropagation(); finish(null);
                    } else if (e.key === 'Enter') {
                        // Enter commits the primary action (not from a button,
                        // which has its own click). Skip inside a textarea.
                        const t = e.target;
                        if (t && t.tagName === 'TEXTAREA') return;
                        if (primarySpec) { e.preventDefault(); choose(primarySpec); }
                    }
                };
                // Backdrop click (outside the modal) cancels; a mousedown inside
                // the modal is swallowed so it never reaches the desktop beneath.
                overlay.addEventListener('mousedown', function (e) {
                    if (e.target === overlay) finish(null);
                });
                modal.addEventListener('mousedown', function (e) {
                    e.stopPropagation();
                });
                document.addEventListener('keydown', onKey, true);
                document.body.appendChild(overlay);
                overlay.classList.add('open');
                // Focus: first field (text selected for easy replace), else the
                // primary button.
                if (fieldInputs.length) {
                    try { fieldInputs[0].input.focus();
                          fieldInputs[0].input.select(); } catch (_) {}
                } else {
                    const pb = foot.querySelector('button.primary')
                        || foot.querySelector('button');
                    if (pb) { try { pb.focus(); } catch (_) {} }
                }
            });
        }

        // Single text field -> the typed string, or null on cancel/Escape.
        function openTextPrompt(opts) {
            opts = opts || {};
            return openDialog({
                title: opts.title || 'Enter a value',
                fields: [{
                    key: 'text',
                    label: opts.label || '',
                    value: opts.value || '',
                    placeholder: opts.placeholder || '',
                    validate: opts.validate,
                }],
                buttons: [
                    { label: opts.okLabel || 'OK', value: true, primary: true },
                    { label: 'Cancel', value: false },
                ],
            }).then(function (r) {
                return (r && r.value) ? r.fields.text : null;
            });
        }

        // Confirm box -> true on OK, false on Cancel/Escape/backdrop.
        function openConfirmDialog(opts) {
            opts = opts || {};
            return openDialog({
                title: opts.title || 'Confirm',
                body: function (c) {
                    const p = document.createElement('div');
                    p.className = 'app-dialog-msg';
                    p.textContent = opts.message || '';
                    c.appendChild(p);
                },
                buttons: [
                    { label: opts.okLabel || 'OK', value: true, primary: true,
                      danger: !!opts.danger },
                    { label: 'Cancel', value: false },
                ],
            }).then(function (r) { return !!(r && r.value); });
        }

        // True while a styled dialog is live (the _dlgFinish singleton is set).
        // The keybinding dispatcher consults this to suppress modifier-hotkey
        // actions under an open dialog, so an action can't open a second dialog
        // that would silently cancel the first.
        function isAppDialogOpen() { return !!_dlgFinish; }

        // Read-only info modal (Properties): a list of {k,v} rows + a Close.
        function openInfoModal(opts) {
            opts = opts || {};
            return openDialog({
                title: opts.title || 'Properties',
                body: function (c) {
                    const tbl = document.createElement('div');
                    tbl.className = 'app-dialog-rows';
                    for (const row of (opts.rows || [])) {
                        const r = document.createElement('div');
                        r.className = 'app-dialog-row';
                        const k = document.createElement('span');
                        k.className = 'app-dialog-k';
                        k.textContent = (row.k != null) ? String(row.k) : '';
                        const v = document.createElement('span');
                        v.className = 'app-dialog-v';
                        v.textContent = (row.v != null) ? String(row.v) : '';
                        r.appendChild(k);
                        r.appendChild(v);
                        tbl.appendChild(r);
                    }
                    c.appendChild(tbl);
                },
                buttons: [{ label: 'Close', value: true, primary: true }],
            }).then(function () {});
        }

        // ---- live progress modal (#109) -----------------------------------
        // A cross-host transfer / in-app download shows this while it runs: an
        // accurate byte-based percent bar + a working Cancel that aborts the
        // in-flight chunk loop. Deliberately its OWN .app-dialog-overlay element
        // and NOT registered with the _dlgFinish singleton above — so a stacked
        // Overwrite confirm (openDialog, same z-index 185000, appended LATER in
        // the DOM, thus painted on top) renders over this window without
        // cancelling it, and this window survives a fumbled Escape (no Escape /
        // no backdrop-click cancel here; Cancel button only, so a mis-key can't
        // kill a transfer mid-flight).
        //
        // openProgressDialog({title, name, from, to}) -> a live handle:
        //   { signal, update(done,total), close() }
        //   signal          — the AbortController.signal handed to the chunk loop
        //                     (opts.signal in transferTo / progress.signal in
        //                     downloadRow). Cancel calls ctrl.abort().
        //   update(done,total) — byte counters only (never string concat): fill
        //                     width = done/total (guarded total<=0 -> 100%),
        //                     refresh the meta line (percent · done/total ·
        //                     throughput · ~ETA from performance.now()). No-op
        //                     once closed, so a late progress tick can't touch a
        //                     removed node.
        //   close()         — remove the overlay; idempotent (safe from a finally
        //                     AND after a Cancel).
        function openProgressDialog(opts) {
            opts = opts || {};
            const ctrl = new AbortController();
            const t0 = performance.now();
            let closed = false;

            // Compact byte + ETA formatting, local to this fragment (the progress
            // meta line is its only consumer). fmtBytes covers B..TB; fmtEta is a
            // rough s / m+s readout, blank when it can't be estimated.
            const fmtBytes = function (n) {
                if (!(n > 0)) return '0 B';
                const units = ['B', 'KB', 'MB', 'GB', 'TB'];
                let i = 0, v = n;
                while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
                return (i === 0 ? v : v.toFixed(1)) + ' ' + units[i];
            };
            const fmtEta = function (secs) {
                if (!(secs > 0) || !isFinite(secs)) return '';
                secs = Math.round(secs);
                if (secs < 60) return secs + 's';
                const m = Math.floor(secs / 60), s = secs % 60;
                return m + 'm' + (s < 10 ? '0' : '') + s + 's';
            };

            const overlay = document.createElement('div');
            overlay.className = 'app-dialog-overlay';
            const modal = document.createElement('div');
            modal.className = 'app-dialog';
            overlay.appendChild(modal);

            if (opts.title) {
                const head = document.createElement('div');
                head.className = 'set-head';
                head.textContent = opts.title;
                modal.appendChild(head);
            }
            const body = document.createElement('div');
            body.className = 'app-dialog-body';
            modal.appendChild(body);

            if (opts.name) {
                const nm = document.createElement('div');
                nm.className = 'app-dialog-msg';
                nm.textContent = String(opts.name);
                body.appendChild(nm);
            }
            if (opts.from || opts.to) {
                const route = document.createElement('div');
                route.className = 'app-dialog-progress-route';
                route.textContent = String(opts.from || '') + ' → '
                    + String(opts.to || '');
                body.appendChild(route);
            }
            const track = document.createElement('div');
            track.className = 'app-dialog-progress';
            const fill = document.createElement('div');
            fill.className = 'app-dialog-progress-fill';
            fill.style.width = '0%';
            track.appendChild(fill);
            body.appendChild(track);

            const meta = document.createElement('div');
            meta.className = 'app-dialog-progress-meta';
            meta.textContent = '0%';
            body.appendChild(meta);

            const foot = document.createElement('div');
            foot.className = 'set-foot app-dialog-foot';
            const cancelBtn = document.createElement('button');
            cancelBtn.type = 'button';
            cancelBtn.classList.add('danger');
            cancelBtn.textContent = 'Cancel';
            cancelBtn.addEventListener('click', function (e) {
                e.stopPropagation();
                if (ctrl.signal.aborted) return;
                try { ctrl.abort(); } catch (_) {}
                cancelBtn.disabled = true;
                cancelBtn.textContent = 'Cancelling…';
            });
            foot.appendChild(cancelBtn);
            modal.appendChild(foot);

            // Swallow a mousedown inside the box so it never reaches the desktop
            // beneath; NO backdrop-click / Escape handler (Cancel button only).
            modal.addEventListener('mousedown', function (e) {
                e.stopPropagation();
            });

            document.body.appendChild(overlay);
            overlay.classList.add('open');

            const update = function (done, total) {
                if (closed) return;
                done = (done > 0) ? done : 0;           // clamp NaN / negatives
                const hasTotal = (total > 0);
                let pct = hasTotal ? (done / total) * 100 : 100;
                if (pct > 100) pct = 100;
                fill.style.width = pct.toFixed(1) + '%';
                const elapsed = (performance.now() - t0) / 1000;   // seconds
                const rate = (elapsed > 0) ? (done / elapsed) : 0; // bytes/s
                let line = Math.round(pct) + '%';
                line += hasTotal
                    ? ' · ' + fmtBytes(done) + ' / ' + fmtBytes(total)
                    : ' · ' + fmtBytes(done);
                if (rate > 0) line += ' · ' + fmtBytes(rate) + '/s';
                if (rate > 0 && hasTotal && total > done) {
                    const eta = fmtEta((total - done) / rate);
                    if (eta) line += ' · ~' + eta;
                }
                meta.textContent = line;
            };
            const close = function () {
                if (closed) return;
                closed = true;
                if (overlay.parentNode) {
                    overlay.parentNode.removeChild(overlay);
                }
            };
            return { signal: ctrl.signal, update: update, close: close };
        }
