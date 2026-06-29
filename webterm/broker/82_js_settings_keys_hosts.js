        // ---- settings: keybindings (task 4) -------------------------------
        // One row per action: label | current combo | Set | Clear. Edits the
        // active settingsTarget's keybindings, so they are per-host like the
        // rest (the LOCAL host's map is what the dispatcher reads live).
        function renderKeybindings() {
            const t = settingsTarget;
            setKeybindingsEl.textContent = '';
            if (!t) return;
            const map = t.s.keybindings || {};
            for (const act of KEY_ACTIONS) {
                const row = document.createElement('div');
                row.className = 'kb-row';
                const label = document.createElement('span');
                label.className = 'kb-label';
                label.textContent = act.label;
                const combo = document.createElement('span');
                const cur = map[act.id] || '';
                combo.className = 'kb-combo' + (cur ? '' : ' unset');
                combo.textContent = cur || 'unset';
                const setBtn = document.createElement('button');
                setBtn.type = 'button';
                setBtn.textContent = 'Set';
                setBtn.addEventListener('click', () => {
                    // Cancel any other in-progress capture first.
                    _kbRecording = null;
                    document.querySelectorAll('.kb-combo.recording')
                        .forEach(el => el.classList.remove('recording'));
                    combo.classList.add('recording');
                    combo.textContent = 'press keys…';
                    _kbRecording = { actionId: act.id, done: (newCombo) => {
                        combo.classList.remove('recording');
                        // Re-resolve the target (a tab switch mid-capture is
                        // guarded by _kbRecording=null on switch, so this is the
                        // same target that was open when Set was pressed).
                        const tt = settingsTarget;
                        if (!tt) { renderKeybindings(); return; }
                        tt.s.keybindings[act.id] = newCombo;
                        tt.save();
                        renderKeybindings();
                    } };
                });
                const clearBtn = document.createElement('button');
                clearBtn.type = 'button';
                clearBtn.textContent = 'Clear';
                clearBtn.addEventListener('click', () => {
                    const tt = settingsTarget;
                    if (!tt) return;
                    delete tt.s.keybindings[act.id];
                    tt.save();
                    renderKeybindings();
                });
                row.appendChild(label);
                row.appendChild(combo);
                row.appendChild(setBtn);
                row.appendChild(clearBtn);
                setKeybindingsEl.appendChild(row);
            }
        }

        // ---- settings: hosts --------------------------------------------------
        // Unlike the rest of the modal, adding a host is an explicit
        // button: auto-persist on change would start polling half-typed
        // URLs. Edit reuses the same form, keeping the host id (and with
        // it every '<id>:*' per-session pref).
        const hostsListEl = document.getElementById('set-hosts-list');
        const hostLabelEl = document.getElementById('set-host-label');
        const hostUrlEl = document.getElementById('set-host-url');
        const hostPassEl = document.getElementById('set-host-pass');
        const hostErrEl = document.getElementById('set-host-err');
        const hostAddBtn = document.getElementById('set-host-add');
        let editingHostId = null;

        function setHostError(msg) {
            hostErrEl.textContent = msg || '';
            hostErrEl.classList.toggle('show', !!msg);
        }
        function resetHostForm() {
            editingHostId = null;
            hostAddBtn.textContent = 'add';
            hostLabelEl.value = '';
            hostUrlEl.value = '';
            hostPassEl.value = '';
            setHostError('');
        }

        function normalizeHostUrl(raw) {
            // -> canonical origin ('http://host:4445') or null. http(s)
            // only; a bare 'host:4445' gets http:// prefixed.
            let s = String(raw || '').trim();
            if (!s) return null;
            if (!/^https?:\/\//i.test(s)) s = 'http://' + s;
            let u;
            try { u = new URL(s); } catch (_) { return null; }
            if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
            return u.origin;
        }

        function mintHostId() {
            // Random short id, stable across URL edits so host-qualified
            // prefs survive renames. 'h' prefix keeps it from ever parsing
            // as a bare window id.
            let id = 'h';
            const abc = 'abcdefghijklmnopqrstuvwxyz0123456789';
            for (let i = 0; i < 9; i++) {
                id += abc.charAt(Math.floor(Math.random() * abc.length));
            }
            return id;
        }

        function defaultHostLabel(url) {
            return url.replace(/^https?:\/\//, '');
        }

