---
scope: core
description: Secret-reference URI formats and the folder/group hierarchy secrets live in (1Password, KeePass, Key Vault, Keychain) — how identity/accounts/*.yaml resolve to real vault locations
---
# Secret Placement & Reference URIs

`identity/accounts/*.yaml` never hold raw secrets — only **reference URIs** that
point at where the credential actually lives. This rule fixes (a) the URI format
per backend and (b) the folder/group hierarchy, so a reference resolves
deterministically and a human finds the secret in the vault without guessing.

## Supported schemes & URI format

| Backend | URI format | Resolves to |
|---|---|---|
| Azure Key Vault | `azure-keyvault://<vault>/<secret-name>` | secret in that vault |
| macOS Keychain | `keychain://<service>[/<account>]` | generic-password item |
| 1Password | `1password://<vault>/<item>/<field>` | one field of one item |
| KeePass (.kdbx) | `keepass://<db>/<group-path>/<entry>/<field>` | one field of one entry |

- `<field>` defaults to `password` if omitted.
- `<db>` for KeePass is the logical database name (e.g. `personal`, `<org>`), mapped
  to a real `.kdbx` path in `bridge-config.yaml` under `secrets.keepass.<db>.path`
  (never hardcode the filesystem path in the account file).
- `<group-path>` is slash-separated KeePass groups, e.g. `<org>/<customer>`.

## Folder / group hierarchy (the placement convention)

Group secrets **by owner-org first, then by service/customer, then by role** —
the same axis the Bridge uses for scope. This keeps one customer's secrets in one
subtree and makes access-scoping and rotation reviewable.

```
<org>/<customer-or-service>/<role>
```

Examples (1Password vault `<ORG>` / KeePass group path):
```
<org>/<customer>/azure-sp-outbound   → 1password://<ORG>/<customer>-azure-sp-outbound/password
<org>/<customer>/storage-api         → keepass://<org>/<ORG>/<customer>/storage-api/password
<org>/_org/cloudflare-token          → 1password://<ORG>/cloudflare-token/credential
<org>/<service>/hf-token             → keepass://personal/<org>/<service>/hf-token/token
```

Rules:
- **One item = one credential.** Don't stuff multiple services in one item; the
  URI addresses a single field.
- **Org/customer isolation.** A customer's secrets live under `<org>/<customer>/…`
  only — never mixed into a shared/root group. This mirrors the data-isolation
  boundary (docs/multi-instance.md): a per-customer Bridge instance references only
  its own subtree.
- **Namespaced entry names.** Prefix the entry with the customer when the vault is
  flat (1Password vaults have no nested groups): `<customer>-azure-sp-outbound`,
  not `azure-sp`.
- **Reference must resolve.** The `<vault>/<group-path>/<entry>` in the URI must
  match the real location. If you move a secret, update the account file's URI.

## Retrieval

Skills resolve a URI through the matching backend tool — `az keyvault secret show`,
`security find-generic-password`, the 1Password CLI (`op read`), or the KeePass
mechanism (`secrets` skill / `keepassxc-cli`). A skill NEVER prints the resolved
secret into the conversation or a file; it uses it in the operation and discards it.

## Hard rules

- Raw secret values never appear in any repo file, commit, log, or artifact.
- The account YAML is `scope: user`/`org` and often gitignored; even so, it holds
  **only** the URI, never the value — so a leak of the file leaks a pointer, not a
  credential.
- Rotation updates the vault; the URI (and thus the account file) usually stays
  unchanged. If the entry is renamed/moved, update the URI in the same change.
