# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **StackStorm pack** with two actions:

**`audit_servers`** — folder-based audit:
1. Authenticates to Delinea Secret Server using a username + password OAuth2 flow
2. Enumerates every secret in a named folder
3. SSH-probes each server to collect its live hostname and IP address

**`run_command`** — inventory-based command runner:
1. Accepts a list of FQDNs and a shell command
2. For each host, looks up `<short>_root` then `<short>_sysadmin` in Secret Server
3. Runs the command via SSH and returns a table-formatted result

## Commands

### Deploy to StackStorm

```bash
sudo cp -r server_audit /opt/stackstorm/packs/
sudo st2 run packs.setup_virtualenv packs=server_audit
sudo st2ctl reload --register-all
st2 action list --pack server_audit   # verify
```

### KV setup (required before first run)

```bash
st2 key set ss_username "AnsibleAPI"
st2 key set ss_password "<password>" --encrypt
st2 key set ss_url "https://your-instance.secretservercloud.com"
st2 key list   # verify
```

### Run the actions

```bash
# Folder audit
st2 run server_audit.audit_servers folder_name="Azure-Linux"
st2 run server_audit.audit_servers folder_name="Azure-Linux" ssh_port=2222 ssh_timeout=20

# Run a command against a host inventory
st2 run server_audit.run_command \
  hosts='["hqora-prd-app02.wescodist.com","hqora-prd-app03.wescodist.com"]' \
  command="uptime"

# Use non-default KV key names (e.g. for a prod vs staging split)
st2 run server_audit.run_command \
  hosts='["hqora-prd-app02.wescodist.com"]' \
  command="df -h" \
  ss_kv_username=prod_ss_username \
  ss_kv_password=prod_ss_password \
  ss_kv_url_key=prod_ss_url
```

There are no automated tests. Manual validation is done by running the action against a real Secret Server folder.

## Architecture

### File layout

```
actions/
  audit_servers.yaml   # parameter schema (folder_name is the only required field)
  audit_servers.py     # single class: AuditServersAction
  run_command.yaml     # parameter schema (hosts array + command are required)
  run_command.py       # single class: RunCommandAction
pack.yaml              # pack metadata
requirements.txt       # paramiko, requests
```

### Execution flow in `AuditServersAction.run()`

```
ST2 KV (client_id, client_secret, url)
        │
        ▼
POST /oauth2/token  →  Bearer token
        │
        ▼
GET /api/v1/folders?filter.searchText=<folder_name>  →  folder_id
        │
        ▼
GET /api/v1/secrets?filter.folderId=<id>  (paginated, 100/page)
        │
        ▼
for each secret:
  GET /api/v1/secrets/<id>  →  extract machine / username / password fields
  SSH connect → run `hostname; awk .../etc/os-release || uname -sr`
  → result dict (non-fatal: errors are captured, run continues)
```

### Key design decisions

- **All config comes from ST2 KV**, never from action parameters directly. The `ss_kv_*` parameters are just the *names* of the KV keys, not the values. `ss_password` is read with `decrypt=True`; everything else is plain text.

- **Folder resolution** does a case-insensitive exact match first; falls back to the first search result with a warning. Raises if no results at all.

- **Secret field lookup** (`_get_field_value`) checks both `slug` and `fieldName` (case-insensitive) and accepts multiple alias slugs (e.g. `["machine", "host", "server"]`). Secrets must follow the Azure-Linux-Root template; missing fields produce a per-host failure, not an abort.

- **SSH errors are non-fatal**. Each host produces a result dict with `status: "failed"` and an `error` string. Only Secret Server authentication or folder-not-found errors abort the entire run.

- **`audit_servers.yaml` is the source of truth for parameters.** When adding or removing parameters, update both the YAML schema and the `run()` method signature together.

### Output shape

```json
[
  {
    "secret_name": "w2lcslogcl06p_root",
    "target_host": "w2lcslogcl06p.wescodist.com",
    "ssh_hostname": "w2lcslogcl06p",
    "ip_address": "10.1.2.3",
    "status": "success",
    "error": null
  },
  {
    "secret_name": "w2lcslogcl07p_root",
    "target_host": "w2lcslogcl07p.wescodist.com",
    "ssh_hostname": null,
    "ip_address": null,
    "status": "failed",
    "error": "Authentication failed for user 'root' — check the password in Secret Server."
  }
]
```

`target_host` is always the `machine` field from the secret, even on failure. `ssh_hostname` and `ip_address` are `null` on failure. A failed host does not stop the run.

---

### Execution flow in `RunCommandAction.run()`

```
hosts list + command (action parameters)
        │
        ▼
ST2 KV (ss_username, ss_password, ss_url)
        │
        ▼
POST /oauth2/token  →  Bearer token
        │
        ▼
for each host (FQDN):
  short = host.split(".")[0]   # e.g. "hqora-prd-app02"
  for suffix in ("_root", "_sysadmin"):
    GET /api/v1/secrets?filter.searchText=<short><suffix>
        → exact-name match  →  GET /api/v1/secrets/<id>
        → extract username + password fields
    SSH connect + exec_command(command)
        → success: record output, break
        → auth failure: try next suffix
        → connection/DNS/timeout error: record failure, break (no retry)
  → result dict per host
```

### Key design decisions (`run_command`)

- **Short name derivation**: `host.split(".")[0]` strips the domain, so `hqora-prd-app02.wescodist.com` becomes `hqora-prd-app02`. The secret search is `hqora-prd-app02_root`.

- **Credential priority**: `_root` is always tried first. Only auth failures (`paramiko.AuthenticationException`) and missing/unfound secrets cause a fallback to `_sysadmin`. Network errors (timeout, DNS, refused) fail immediately — different credentials won't help.

- **Secret search**: uses `GET /api/v1/secrets?filter.searchText=<name>` and filters the response for an exact case-insensitive name match. Returns `None` (not an error) when the secret doesn't exist.

- **`run_command.yaml` is the source of truth for parameters.** Keep YAML schema and `run()` signature in sync.

### Output shape (`run_command`)

```json
[
  {
    "host": "hqora-prd-app02.wescodist.com",
    "user": "root",
    "secret_used": "hqora-prd-app02_root",
    "command_output": "15:42:01 up 42 days,  3:17,  1 user,  load average: 0.01",
    "status": "success",
    "error": null
  },
  {
    "host": "hqora-prd-app03.wescodist.com",
    "user": "sysadmin",
    "secret_used": "hqora-prd-app03_sysadmin",
    "command_output": "15:42:03 up 10 days,  1:05,  0 users,  load average: 0.00",
    "status": "success",
    "error": null
  },
  {
    "host": "hqora-prd-app04.wescodist.com",
    "user": null,
    "secret_used": null,
    "command_output": null,
    "status": "failed",
    "error": "No usable credentials found for 'hqora-prd-app04.wescodist.com' (tried _root and _sysadmin)."
  }
]
```

The logger also prints a table with columns: Host | User | Secret Used | Command Output | Status | Error.

