        // ---- ctx.dialog: the queued, per-dialog-handle surface (#202) -------
        //
        //     const d = ctx.dialog.open({
        //         title: 'Unlock registry',
        //         fields: [{ name: 'pass', label: 'Passphrase',
        //                    type: 'password' }],
        //     });                       // the handle comes back SYNCHRONOUSLY
        //     d.close();                // acts on THIS dialog only
        //     d.replace(spec);          // acts on THIS dialog only
        //     const vals = await d.result;   // {pass:'...'} | null (dismissed)
        //
        // WHAT IT REPLACES. mods/host-registry hand-builds its password inputs
        // (a body callback + document.createElement('input') + type='password')
        // and carries an `_encBusy` single-flight flag whose entire job is to
        // stop a SECOND passphrase prompt, because core's openDialog is a
        // singleton: opening a dialog CANCELS the live one, so a mod that
        // prompts while another prompt is up silently dismisses it (the first
        // caller sees a null it can only read as "the user cancelled"). That
        // workaround class is what this surface kills. The mod migration is
        // #204's, not this fragment's: nothing here changes a mod, and every
        // existing openDialog call site is untouched.
        //
        // MOD-FACING OPENS ALWAYS QUEUE. There is no cancel-the-current-dialog
        // mode for mods at all -- not a flag, not an option -- so one mod can
        // never dismiss another mod's prompt, or core's. A second open while
        // one is on screen waits its turn and is shown when the first settles.
        // CORE KEEPS ITS OWN BEHAVIOUR: openDialog (69) is not modified, so a
        // core dialog still cancels whatever is live, including a mod's. That
        // asymmetry is deliberate and NAMED: core is the application, a mod is
        // a guest. Its consequence is the reason the pump below refuses to
        // show anything while a foreign dialog is live -- otherwise the next
        // queue entry would open straight into core's prompt and cancel IT,
        // which is exactly the defect this fragment exists to remove.
        //
        // THE HANDLE IS RETURNED SYNCHRONOUSLY, before display, so `close` and
        // `replace` exist during the whole queued life of the dialog and not
        // only once it is on screen. Both act on THIS entry: closing a queued
        // entry drops it (it is never shown, and its result resolves null),
        // closing the shown entry finishes only that dialog, and `replace` on a
        // queued entry just swaps the spec it will eventually be shown with.
        //
        // A DEAD MOD MUST NOT WEDGE THE QUEUE. On teardown a mod's queued
        // entries are dropped and its on-screen dialog is closed, so the next
        // owner's entry is shown immediately. An open() from an already-dead
        // activation is refused (a handle whose result is already null) rather
        // than queued behind nothing.
        //
        // PASSWORD VALUES: type:'password' produces a real password input, and
        // the value is handed to exactly one place -- the caller's `result`.
        // Core never logs it (nothing in this range passes a collected value
        // to console.*), never persists it (no prefs / state write lives here),
        // and drops its own reference the moment the dialog settles.
        //
        // Its own fragment for the reason 86e-86l each got one: 86c is at the
        // #68 2500-line cap and the rule there is split, never trim. Ordered
        // after 86c because it uses that fragment's registry, and before the
        // mod-script splice because a mod's init reads the finished ctx.
        const _MOD_DIALOG_QUEUE = [];
        let _modDialogActive = null;
        let _modDialogPumpTimer = null;

        // A foreign dialog is anything core (or 69's own wrappers) put up that
        // this queue did not. Showing into one would cancel it, so the pump
        // waits instead. `_dlgFinish` is 69's live-dialog handle; a page
        // assembled without that fragment simply has no foreign dialog.
        function _modDialogForeignLive() {
            try {
                return (typeof _dlgFinish === 'function' && !!_dlgFinish);
            } catch (_) { return false; }
        }

        // Settle ONE entry exactly once. `_modDialogActive` is cleared only if
        // this entry is the one on screen, so a settle on a queued entry (a
        // close() before display, a teardown drop) cannot free the slot the
        // shown dialog still occupies.
        function _modDialogSettle(entry, values) {
            if (!entry || entry.settled) return false;
            entry.settled = true;
            entry.fin = null;
            entry.spec = null;          // drop core's reference to the spec
            // BLANK the fields before dropping them. Setting `inputs = null`
            // drops OUR reference, but a detached input element retained by
            // anything else still carries the typed password in its .value.
            // Clearing costs nothing and removes the copy we created by
            // building the field in the first place. (This is hygiene, not a
            // boundary: the DOM is shared, so a mod that wants the value while
            // the dialog is open can read it -- see the header.)
            if (entry.inputs) {
                for (const fi of entry.inputs) {
                    try { if (fi && fi.el) fi.el.value = ''; } catch (_) {}
                }
            }
            entry.inputs = null;        // ...and to the live password inputs
            if (_modDialogActive === entry) _modDialogActive = null;
            try { entry.resolve(values); } catch (_) {}
            _modDialogPumpSoon();
            return true;
        }

        // Build the rows for one spec into core's body container. Deliberately
        // NOT openDialog's `fields` (which is text-only), and deliberately the
        // same CSS groups it uses, so a mod dialog is visually the core dialog
        // rather than a second look.
        function _modDialogBuildFields(wrap, spec) {
            const inputs = [];
            const fields = Array.isArray(spec && spec.fields) ? spec.fields : [];
            for (const f of fields) {
                if (!f || typeof f !== 'object') continue;
                const name = (typeof f.name === 'string') ? f.name : '';
                if (!name) continue;
                const row = document.createElement('div');
                row.className = 'set-row app-dialog-field';
                if (f.label) {
                    const lab = document.createElement('label');
                    lab.textContent = String(f.label);
                    row.appendChild(lab);
                }
                let el;
                if (f.type === 'select') {
                    el = document.createElement('select');
                    const opts = Array.isArray(f.options) ? f.options : [];
                    for (const o of opts) {
                        const opt = document.createElement('option');
                        const val = (o && typeof o === 'object') ? o.value : o;
                        opt.value = (val != null) ? String(val) : '';
                        opt.textContent = (o && typeof o === 'object'
                            && o.label != null) ? String(o.label) : opt.value;
                        el.appendChild(opt);
                    }
                } else {
                    el = document.createElement('input');
                    // Only 'password' is special-cased; every other type falls
                    // back to text rather than being passed through, so a spec
                    // cannot smuggle an arbitrary input type onto the page.
                    el.type = (f.type === 'password') ? 'password' : 'text';
                    if (f.placeholder) el.placeholder = String(f.placeholder);
                }
                if (f.value != null) el.value = String(f.value);
                row.appendChild(el);
                wrap.appendChild(row);
                inputs.push({ name: name, el: el });
            }
            return inputs;
        }

        // The committed values, read off the live inputs. Returns null once the
        // entry has been torn down, which is what makes a commit racing a
        // teardown resolve `null` (dismissed) instead of a half-read object.
        function _modDialogCollect(entry) {
            if (!entry || !entry.inputs) return null;
            // Object.create(null): a field legitimately named `__proto__`
            // assigned through a plain object's legacy setter produces NO own
            // property at all, so the collected value would go missing while
            // `values.__proto__` handed back Object.prototype. A null-prototype
            // target makes the name ordinary.
            const out = Object.create(null);
            for (const fi of entry.inputs) {
                let v = '';
                try { v = fi.el.value; } catch (_) { v = ''; }
                out[fi.name] = (v != null) ? String(v) : '';
            }
            return out;
        }

        // Show ONE entry through core's openDialog. The finish handle is
        // captured immediately after the call, while `_dlgFinish` still names
        // THIS dialog -- that capture is what makes close()/replace() per
        // dialog: they act through the captured handle and only while core
        // still holds it, so a mod can never finish a dialog that is no longer
        // the one it opened.
        function _modDialogShow(entry) {
            const spec = entry.spec || {};
            _modDialogActive = entry;
            let p;
            try {
                p = openDialog({
                    title: spec.title,
                    body: function (wrap) {
                        entry.inputs = _modDialogBuildFields(wrap, spec);
                    },
                    buttons: [
                        { label: (typeof spec.cancelLabel === 'string')
                            ? spec.cancelLabel : 'Cancel', value: false },
                        { label: (typeof spec.submitLabel === 'string')
                            ? spec.submitLabel : 'OK',
                          value: true, primary: true },
                    ],
                });
            } catch (e) {
                try { console.error('[dialog] open failed:', e); } catch (_) {}
                _modDialogSettle(entry, null);
                return;
            }
            try {
                entry.fin = (typeof _dlgFinish === 'function')
                    ? _dlgFinish : null;
            } catch (_) { entry.fin = null; }
            Promise.resolve(p).then(function (r) {
                if (entry.replacing) {
                    // A replace() finished the old dialog on purpose: this
                    // settlement is not the caller's answer. Re-show the SAME
                    // entry with its new spec, still holding the active slot.
                    entry.replacing = false;
                    entry.inputs = null;
                    if (entry.settled) return;
                    _modDialogShow(entry);
                    return;
                }
                const ok = !!(r && r.value === true);
                const values = ok ? _modDialogCollect(entry) : null;
                _modDialogSettle(entry, values);
            }, function (e) {
                try { console.error('[dialog] failed:', e); } catch (_) {}
                _modDialogSettle(entry, null);
            });
        }

        // The pump. Drops dead / already-closed entries, refuses to show while
        // a dialog is on screen (ours or core's), and re-arms a short poll when
        // it is blocked by a FOREIGN dialog -- core's dialog has no completion
        // hook this fragment may add without changing core's behaviour, so the
        // queue waits for it politely rather than cancelling it.
        function _modDialogPump() {
            _modDialogPumpTimer = null;
            if (_modDialogActive) return;
            while (_MOD_DIALOG_QUEUE.length) {
                const entry = _MOD_DIALOG_QUEUE[0];
                if (entry.settled) { _MOD_DIALOG_QUEUE.shift(); continue; }
                if (entry.rec && entry.rec.unloading) {
                    _MOD_DIALOG_QUEUE.shift();
                    _modDialogSettle(entry, null);
                    continue;
                }
                if (_modDialogForeignLive()) {
                    _modDialogPumpLater();
                    return;
                }
                _MOD_DIALOG_QUEUE.shift();
                _modDialogShow(entry);
                return;
            }
        }
        function _modDialogPumpSoon() {
            if (_modDialogPumpTimer) return;
            _modDialogPumpTimer = -1;
            Promise.resolve().then(function () {
                if (_modDialogPumpTimer === -1) _modDialogPump();
            });
        }
        function _modDialogPumpLater() {
            if (_modDialogPumpTimer && _modDialogPumpTimer !== -1) return;
            _modDialogPumpTimer = setTimeout(_modDialogPump, 150);
        }

        // close(): drop a queued entry, or finish the shown one -- THIS one.
        // The captured finish is used only while core still holds it; if core
        // has since replaced it, our dialog is already gone and the settle has
        // already run (or is about to), so there is nothing to cancel.
        function _modDialogClose(entry) {
            if (!entry || entry.settled) return false;
            const fin = entry.fin;
            let live = false;
            try {
                live = !!fin && (typeof _dlgFinish === 'function')
                    && _dlgFinish === fin;
            } catch (_) { live = false; }
            entry.replacing = false;
            if (live) { try { fin(null); } catch (_) {} }
            return _modDialogSettle(entry, null);
        }

        // replace(): swap THIS entry's spec. Queued, that is all it is; shown,
        // the live dialog is finished with `replacing` set so the settlement is
        // swallowed and the entry is re-shown from its new spec without ever
        // releasing the active slot -- so a replace cannot let another mod's
        // queued dialog jump in front of it.
        function _modDialogReplace(entry, spec) {
            if (!entry || entry.settled) return false;
            entry.spec = (spec && typeof spec === 'object') ? spec : {};
            if (_modDialogActive !== entry) return true;
            const fin = entry.fin;
            let live = false;
            try {
                live = !!fin && (typeof _dlgFinish === 'function')
                    && _dlgFinish === fin;
            } catch (_) { live = false; }
            if (!live) return true;   // already gone: the settle path owns it
            entry.replacing = true;
            try { fin(null); } catch (_) { entry.replacing = false; }
            return true;
        }

        // Teardown. A dead mod's queued entries are dropped and its on-screen
        // dialog is closed, so the queue never waits on an activation that can
        // no longer answer. Every dropped entry resolves null -- the same value
        // a dismissal produces, which is the outcome every caller of a prompt
        // already handles.
        function _dropModDialogs(rec) {
            let n = 0;
            for (const entry of _MOD_DIALOG_QUEUE.slice()) {
                if (entry.rec !== rec || entry.settled) continue;
                if (_modDialogSettle(entry, null)) n++;
            }
            const active = _modDialogActive;
            if (active && active.rec === rec) {
                if (_modDialogClose(active)) n++;
            }
            _modDialogPumpSoon();
            return n;
        }

        // open(). The handle is built and returned before anything is shown --
        // the entry may sit in the queue for as long as the dialogs ahead of it
        // take, and `close`/`replace` are valid for the whole of that.
        function _openModDialog(rec, spec) {
            let resolve = null;
            const result = new Promise(function (r) { resolve = r; });
            const entry = {
                rec: rec,
                owner: (rec && typeof rec.id === 'string') ? rec.id : '',
                spec: (spec && typeof spec === 'object') ? spec : {},
                resolve: resolve,
                settled: false,
                replacing: false,
                inputs: null,
                fin: null,
            };
            const handle = {
                result: result,
                close: function () { return _modDialogClose(entry); },
                replace: function (next) {
                    return _modDialogReplace(entry, next);
                },
            };
            // A dead activation is refused rather than queued: it would be
            // dropped by the pump on its next turn anyway, and queueing it
            // first means the dialog can FLASH on screen for a mod that is
            // already gone.
            if (rec && rec.unloading) {
                _modDialogSettle(entry, null);
                return handle;
            }
            _MOD_DIALOG_QUEUE.push(entry);
            _modDialogPumpSoon();
            return handle;
        }

        // The extender. The teardown disposer is registered BEFORE the surface
        // that can queue anything is put on the ctx (the house rule: register
        // the disposer before anything that can throw), so there is no window
        // in which a mod can open a dialog that nothing would ever drop.
        function _ctxDialog(ctx, rec) {
            if (typeof ctx.onUnload === 'function') {
                ctx.onUnload(function () { _dropModDialogs(rec); });
            }
            ctx.dialog = {
                open: function (spec) { return _openModDialog(rec, spec); },
            };
        }
        // Guarded like every other registration in an extension fragment: a
        // page assembled without #194's registry or #197's capability map must
        // not throw at load.
        if (typeof _registerCtxExtender === 'function') {
            _registerCtxExtender(_ctxDialog);
        }
        // A NEW top-level family, so it registers its own capability entry
        // beside the surface it names (#197: the map is a true inventory, and
        // the v1 seed loop must stay byte-equal to makeCtx's literal).
        if (typeof _registerModCapability === 'function') {
            _registerModCapability('dialog', 1);
        }
        // ---- end ctx.dialog --------------------------------------------------
