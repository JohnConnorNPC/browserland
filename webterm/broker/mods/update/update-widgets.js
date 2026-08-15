        // ---- update mod: DOM widget helpers (#182 Part 2, atom A2) ----
        // update.js sat at 2495/2500 against the fragment cap. Split out
        // the same way update-policy.js/update-apply.js were: a
        // companion script with NO registerMod call, spliced immediately
        // before update.js in ui.py's _MODS list. Every shipped mod
        // script lands in ONE shared inline <script>, so a top-level
        // declaration here is a name update.js's own closures can still
        // call exactly as before; only WHERE they are defined changed.
        //
        // Unlike update-policy.js/update-apply.js (deliberately pure AND
        // DOM-free, because test_update_fleet.py's node harness executes
        // them against a stub page with no `document`), everything below
        // DOES touch the DOM -- so it ships in its OWN companion rather
        // than either of those: the harness reads update-policy.js and
        // update-apply.js WHOLE and asserts "document.createElement"
        // never appears in what it evaluates. This file is not part of
        // that concatenation.
        //
        // Nothing here may reference anything from update.js's own
        // closure (ctx, checkStateFor, updHost, LOCAL_HOST_ID, hostFetch,
        // policyOps, applyFlow, ...) -- every fact a caller needs is
        // taken as a plain argument, the same PURE-over-its-arguments
        // rule update-policy.js/update-apply.js follow, minus the
        // DOM-free half of it. update.js's renderApplyRow/
        // renderRemoteApplyRow/applyConfirmBody/renderWindow/
        // renderSelfUpdateRow/confirmRemoteSelfUpdate/openUpdateWindow
        // call these exactly as if they were still declared inside it.

        // One DOM element, three optional bits: a class, and text via
        // .textContent -- every caller's `text` came off the network or
        // is user-entered (a broker label), so it is never innerHTML'd.
        function mkEl(tag, cls, text) {
            const e = document.createElement(tag);
            if (cls) e.className = cls;
            if (text !== undefined) e.textContent = text;
            return e;
        }

        // Shared op-note painter for the two apply rows: the op's
        // lines land in `stat`, green/amber banded ('waiting' has
        // no band); returns whether it painted anything.
        function applyOpNotes(stat, op, busy) {
            if (!op || !(busy || ['done', 'timeout', 'failed']
                    .indexOf(op.phase) !== -1)) return false;
            for (const t of op.note) stat.appendChild(mkEl('div', null, t));
            if (op.phase !== 'waiting') stat.classList.add(
                op.phase === 'done' ? 'app-upd-green' : 'app-upd-amber');
            return true;
        }
        const APPLY_BUTTONS = [
            { label: 'Apply and restart', value: true,
              primary: true, danger: true },
            { label: 'Cancel', value: false }];

        // One labelled row. Values go through .textContent — everything
        // here except our own literals came off the network, and a
        // broker label is user-entered text on top of that. rowCls lets
        // the per-broker block widen its own label column without
        // needing a second copy of this function.
        function addRow(body, label, value, cls, rowCls) {
            const row = document.createElement('div');
            row.className = 'app-upd-row' + (rowCls ? ' ' + rowCls : '');
            const k = document.createElement('span');
            k.className = 'app-upd-key';
            k.textContent = label;
            const v = document.createElement('span');
            v.className = 'app-upd-val' + (cls ? ' ' + cls : '');
            v.textContent = value;
            row.appendChild(k);
            row.appendChild(v);
            body.appendChild(row);
            return v;
        }

        function addNote(body, text, cls) {
            const el = document.createElement('div');
            el.className = 'app-upd-note' + (cls ? ' ' + cls : '');
            el.textContent = text;
            body.appendChild(el);
            return el;
        }

        // A section divider. The window now has two kinds of row —
        // facts about the comparison (shared by every broker, because
        // the upstream is one constant repository) and facts about one
        // broker — and a reader who cannot tell them apart would read a
        // shared "Upstream" line as belonging to the row above it.
        function addHead(body, text) {
            const el = document.createElement('div');
            el.className = 'app-upd-head';
            el.textContent = text;
            body.appendChild(el);
            return el;
        }
