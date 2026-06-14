# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **StackStorm pack** with a single action (`audit_servers`) that:
1. Authenticates to Delinea Secret Server using a client_id + client_secret OAuth2 flow
2. Enumerates every secret in a named folder
3. SSH-probes each server to collect its live hostname and OS version

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
st2 key set ss_client_id  "<client-id>"
st2 key set ss_client_secret "<client-secret>" --encrypt
st2 key set ss_url "https://your-instance.secretservercloud.com"
st2 key list   # verify
```

### Run the action

```bash
st2 run server_audit.audit_servers folder_name="Azure-Linux"
st2 run server_audit.audit_servers folder_name="Azure-Linux" ssh_port=2222 ssh_timeout=20

# Use non-default KV key names (e.g. for a prod vs staging split)
st2 run server_audit.audit_servers folder_name="Azure-Linux" \
  ss_kv_client_id=prod_ss_client_id \
  ss_kv_client_secret=prod_ss_client_secret \
  ss_kv_url_key=prod_ss_url
```

There are no automated tests. Manual validation is done by running the action against a real Secret Server folder.

## Architecture

### File layout

```
actions/
  audit_servers.yaml   # parameter schema (folder_name is the only required field)
  audit_servers.py     # single class: AuditServersAction
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

- **All config comes from ST2 KV**, never from action parameters directly. The `ss_kv_*` parameters are just the *names* of the KV keys, not the values. `client_secret` is read with `decrypt=True`; everything else is plain text.

- **Folder resolution** does a case-insensitive exact match first; falls back to the first search result with a warning. Raises if no results at all.

- **Secret field lookup** (`_get_field_value`) checks both `slug` and `fieldName` (case-insensitive) and accepts multiple alias slugs (e.g. `["machine", "host", "server"]`). Secrets must follow the Azure-Linux-Root template; missing fields produce a per-host failure, not an abort.

- **SSH errors are non-fatal**. Each host produces a result dict with `status: "failed"` and an `error` string. Only Secret Server authentication or folder-not-found errors abort the entire run.

- **`audit_servers.yaml` is the source of truth for parameters.** When adding or removing parameters, update both the YAML schema and the `run()` method signature together.

### Output shape

```json
[
  {
    "secret_name": "myserver_root",
    "target_host": "myserver.example.com",
    "ssh_hostname": "myserver",
    "os_version": "Red Hat Enterprise Linux 8.10 (Ootpa)",
    "status": "success",
    "error": null
  }
]
```

`ssh_hostname` and `os_version` are `null` on failure. A failed host does not stop the run.

