# Mod package signing — design note

Status: design only. Nothing described here is implemented. This note settles the
shape so the implementation, when it lands, is not re-argued; it is written
against the code as it exists today in `webterm/broker/modinstall.py`.

Scope: signing an installed mod package (`x-*`) so a broker can decide the
package's provenance *itself*, without trusting whoever handed it over. It is not
a certificate authority, not a store, not revocation infrastructure. Pinned keys,
operator-managed — the same trust shape as a pinned upstream repo.

## 1. What exists today

Two symbols carry the whole of the current integrity story.

`validate_package(manifest, files)` validates one package **entirely in memory**
and returns `(canonical_manifest, records)`. Each entry of `records` is
`{"data": bytes, "sha256": <hex>, "integrity": "sha256-<b64>"}` — so a per-file
SHA-256 over the exact bytes already exists for every file in the package. The
canonical manifest comes from `_canonical_manifest`, which types, caps and
normalizes every field, rejects any key outside the closed `MANIFEST_KEYS`
allowlist, drops `defaultEnabled` and `help.slug`, and returns a fixed-shape dict
(`id, version, ctxVersion, title, description, scripts, styles, requires, tiers,
help`). The canonical manifest — not the payload's own bytes — is what gets
written to `mod.json` and what feeds the hash. The same function validates a wire
install payload and a directory read off disk, so there is exactly one rule set.

`compute_gen(manifest, records)` is the canonicalization to reuse. It is a
SHA-256 over, in order:

1. `json.dumps(manifest, sort_keys=True, separators=(",", ":"),
   ensure_ascii=False).encode("utf-8")` — the canonical manifest, key-sorted,
   no incidental whitespace;
2. `b"\n"`;
3. for each `name` in `sorted(records)`: the UTF-8 name, `b"\x00"`, the ASCII
   lowercase hex `sha256` of that file, `b"\n"`.

So `compute_gen` already binds manifest AND files, with a stable ordering and a
delimiter discipline that keeps a name and a digest from running together. The
signing digest reuses exactly that construction. It does not invent a second one.

## 2. The signing digest: domain-separated, signature-excluding

Two new manifest keys (both would have to be added to `MANIFEST_KEYS`, which is a
closed allowlist today — an unknown key is refused with `unknown_manifest_key`):

- `author` — the key id of the signing key (an identifier, not the key itself);
- `signature` — the detached signature over the signing digest.

The signature cannot cover itself. `compute_gen`'s digest hashes the manifest,
and the manifest is where the signature rides; signing that digest would be
self-referential. So:

**signing digest** = SHA-256 over

```
DOMAIN
0x00
canonical-manifest JSON  (compute_gen's dumps(), with the "signature" and
                          "author" keys REMOVED from the dict before dumping)
0x0A
for name in sorted(records):
    name (utf-8)  0x00  sha256-hex (ascii)  0x0A
```

where `DOMAIN` is a fixed ASCII context string chosen at implementation time
(something of the shape `browserland/mod-package-signature/v1`). The domain
prefix is the only structural addition: it means a signing digest can never
collide with, or be mistaken for, any other digest the codebase produces — in
particular a `gen`, which is the same construction without the prefix and with
the signature fields present. A value that verifies as a signature over one can
never be replayed as the other.

Excluding `signature` and `author` (and nothing else) keeps the digest stable
across the act of signing: the signer computes it from the unsigned manifest, the
verifier computes it from the signed manifest by removing those two keys, and
both get the same bytes. Every other manifest field, and every file's content
digest, stays inside the signature. Adding a file, removing one, renaming one,
changing a byte of one, or editing `requires`/`scripts`/`tiers`/anything else all
break verification.

Removal, not blanking: the keys are deleted from the dict before `dumps()`, so an
unsigned manifest and a signed one produce identical preimages. Blanking to `""`
would only work if every producer agreed on the same placeholder, which is a
second thing to get wrong.

Because per-file `sha256` values already come out of `validate_package`, nothing
new has to be hashed — verification is a dict edit, a `dumps`, and a signature
check over material the validator produced anyway.

## 3. `gen` stays over the FULL manifest

`compute_gen` is left exactly as it is: it hashes the canonical manifest
*including* `signature` and `author`. Consequence: re-signing an otherwise
byte-identical package yields a **new generation**.

That is the correct coupling. `gen` is the store's identity for an artifact — it
keys `index["assets"]` as `"<id>/<gen>/<name>"`, it is what cached URLs point at,
and its existing docstring gives the rule: a mod whose only change is its
`requires` list is a different generation, because reusing the old `gen` would
leave cached URLs pointing at the old graph. A re-sign changes the manifest, and
therefore what a broker will have verified and recorded. Two artifacts that a
verifier treats differently must not share an id. A different key, a rotated key,
a re-issued signature: each is a distinct thing an operator can be asked about,
and each deserves its own `gen` rather than silently mutating one in place.

The reverse coupling — stripping the signature out of `gen` so a re-sign is a
no-op — would make `gen` stop identifying the bytes on disk, which is the one job
it has.

## 4. The enforcement gate: `require_signed_installs`

A broker config flag in `broker_config.json`, alongside the pinned author keys.

- **Default OFF.** Turning it on is a per-broker operator decision. Nothing about
  signing changes behaviour for a broker that has not opted in.
- **On:** validation refuses any package whose manifest does not carry a
  `signature` verifying, under the digest of §2, against a key pinned in that
  broker's config. Refusal happens inside the `validate_package` orbit, before a
  byte is written, so the store is untouched by construction rather than by
  unwinding.
- **No caller-class carve-out.** There is no "this was a local UI click, let it
  through" exception. Two reasons, and the first is sufficient: `/mods/install`
  cannot reliably distinguish a local UI call from a remote or sync-driven one on
  the same route. The second is the principle — the gate is a property of the
  **artifact**, not of the caller. An admin token says who may ask; it says
  nothing about what the package is. An admin who may install is still not an
  authority on whether these bytes came from the author they claim.

### How it composes with the other two layers

Three independent layers, answering three different questions, applied in this
order:

1. **Admin class (#191)** — *may this caller ask for an install at all?* A
   caller-identity check on the route. Answered first, because an unauthorized
   caller's payload should never be inspected.
2. **`require_signed_installs` (this note)** — *is this artifact from a key this
   broker pinned?* Provenance. Answered on the artifact, for every caller alike.
3. **Capability lint (#193)** — *does the manifest's `permissions` declaration
   match what the source text actually reaches?* Truthfulness of a declaration
   the operator is about to consent to.

None of the three subsumes another. A pinned key can sign a mod whose
`permissions` lie (2 passes, 3 refuses). An honest, correctly-declared package
from nobody in particular still fails 2. An admin can be perfectly authorized and
still hand over both (1 passes, 2 and 3 decide). Passing all three is not a
statement that the mod is safe: as the lint's own framing says, a source-text
scanner is not a parser, and installed-but-disabled is not containment. Signing
answers *who vouched for these exact bytes*, and only that.

## 5. The rule to carve now: no fleet install before signing

**No fleet-wide or sync-driven install path ships before signing does.** Not a
"install on all brokers" button, not mod-sync pushing packages, not any
scheduled/replicated install.

Why: today an install is one operator, at one broker, making one trust decision
about one artifact, and the blast radius of a bad call is that broker. A fan-out
path multiplies a single compromise by N — one compromised page, one compromised
sender, or one tampered payload in transit becomes code execution on every broker
in the fleet, with no receiver in a position to disagree. A receiving broker
under a fan-out has no operator at the moment of install; the only thing that can
stand in for that operator's judgement is the ability to verify provenance
**independently of the sender**. That is exactly what a signature is and what a
transport-level check is not: TLS and an authenticated sync channel prove the
sender, not the author.

So the ordering is a hard dependency, not a preference: signing verification plus
the `require_signed_installs` gate land **with, and gate, the first fleet-install
feature**. A fleet feature that shipped first would have to be withdrawn, not
retrofitted.

## 6. Open questions

Named as open. None of these is settled by this note, and the implementation
cannot start until at least the first three are answered.

- **Algorithm.** Ed25519 is the working assumption (small keys, small
  signatures, no parameter choices to get wrong, and it is what the issue
  proposes). Not settled: what the broker uses to verify it. The broker's
  dependency floor and its Windows-and-Linux requirement decide whether that is a
  pure-Python implementation, a vendored one, or a new dependency — and a new
  crypto dependency is a real cost that has not been weighed here.
- **Key distribution.** How a pinned key gets into `broker_config.json` on a
  broker the operator did not hand-configure. Manual paste is the honest starting
  answer and does not scale past a handful of brokers.
- **Who holds the private key.** Author-held (each mod author signs) and
  operator-held (the fleet's own operator counter-signs what it has reviewed)
  are different trust models with different failure modes, and the choice changes
  what a signature *means*. Unresolved.
- **Rotation.** Whether a manifest may carry more than one signature, whether a
  broker may pin more than one key per author, and what happens to already-
  installed generations signed by a key that is no longer pinned. The
  grandfathering rule the capability lint uses — an installed generation keeps
  serving across a rescan and a restart, because a restart that silently drops a
  working mod is an outage, not a check — is the obvious precedent, but it has
  not been ratified for keys.
- **Revocation.** Explicitly out of scope as infrastructure; unpinning a key is
  the only mechanism on offer. What unpinning should do to installed generations
  is the previous question and is open.
- **Signature encoding and size cap.** `signature` and `author` are text fields
  under a closed key allowlist, so both need a concrete encoding (base64 vs hex)
  and a length cap in `_canonical_manifest`, like every other manifest field.
- **Old brokers.** A broker that predates the new keys refuses the manifest
  outright with `unknown_manifest_key` — a signed package will not install on an
  old broker at all. Whether that is acceptable, or whether the keys need a
  tolerated-and-ignored path, is open, and it is the reverse of the capability
  lint's problem (there, an old broker installs *unchecked*).
