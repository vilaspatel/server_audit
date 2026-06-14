# server_audit Pack

Enumerates every secret inside a Delinea Secret Server folder and SSH-probes each
server to verify connectivity and collect the live hostname and IP address.

Each secret is expected to follow the **Azure-Linux-Root** template structure:

| Secret field | Slug | Purpose |
|---|---|---|
| Machine | `machine` | FQDN or IP used as the SSH target |
| Username | `username` | SSH login username |
| Password | `password` | SSH login password |

---

## Prerequisites

| Requirement | Minimum version |
|---|---|
| StackStorm | 3.8+ |
| Python | 3.8 – 3.11 |
| Delinea Secret Server | Any version exposing the v1 REST API |

Network access is required from the StackStorm action runner to:
- The Secret Server HTTPS endpoint
- Port 22 (or custom `ssh_port`) on every target server in the folder

---

## How Authentication Works

The action authenticates using a **Secret Server username and password** stored in
ST2 KV. On each run it performs a password grant to `POST /oauth2/token` and uses
the resulting Bearer token for all API calls — the same credential flow used by the
Delinea python-tss-sdk `PasswordGrantAuthorizer`.

---

## Installation

### 1. Copy the pack onto the StackStorm host

```bash
sudo cp -r server_audit /opt/stackstorm/packs/
```

Or clone directly from your Git remote:

```bash
cd /opt/stackstorm/packs
sudo git clone https://github.com/your-org/server_audit.git
```

### 2. Install Python dependencies

```bash
sudo st2 run packs.setup_virtualenv packs=server_audit
```

### 3. Register the pack with StackStorm

```bash
sudo st2ctl reload --register-all
```

Verify the action is visible:

```bash
st2 action list --pack server_audit
```

---

## Configuration

### ST2 KV Setup

Set these three keys before the first run:

```bash
# Secret Server username (plain text)
st2 key set ss_username "AnsibleAPI"

# Secret Server password (stored encrypted)
st2 key set ss_password "<password>" --encrypt

# Secret Server base URL (plain text)
st2 key set ss_url "https://your-instance.secretservercloud.com"
```

### Verify KV entries

```bash
st2 key list
```

Expected output:

```
+-------------+---------+--------+
| name        | scope   | secret |
+-------------+---------+--------+
| ss_username | system  | False  |
| ss_password | system  | True   |
| ss_url      | system  | False  |
+-------------+---------+--------+
```

---

## Usage

### Run against a folder

```bash
st2 run server_audit.audit_servers folder_name="Azure-Linux"
```

### Run with a custom SSH port and timeout

```bash
st2 run server_audit.audit_servers \
  folder_name="Azure-Linux" \
  ssh_port=2222 \
  ssh_timeout=20
```

### Use non-default KV key names (e.g. prod vs staging)

```bash
st2 run server_audit.audit_servers \
  folder_name="Azure-Linux" \
  ss_kv_username=prod_ss_username \
  ss_kv_password=prod_ss_password \
  ss_kv_url_key=prod_ss_url
```

---

## Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `folder_name` | string | yes | — | Name of the Secret Server folder to enumerate |
| `ss_kv_username` | string | no | `ss_username` | KV key holding the Secret Server username (plain text) |
| `ss_kv_password` | string | no | `ss_password` | KV key holding the Secret Server password (encrypted) |
| `ss_kv_url_key` | string | no | `ss_url` | KV key holding the Secret Server base URL |
| `ssh_port` | integer | no | `22` | SSH port |
| `ssh_timeout` | integer | no | `10` | Connection timeout in seconds |

---

## KV Key Reference

| KV Key | Encrypted | Set by | Purpose |
|---|---|---|---|
| `ss_username` | no | operator | Secret Server login username |
| `ss_password` | yes | operator | Secret Server login password |
| `ss_url` | no | operator | Secret Server base URL |

---

## Output

A list — one entry per secret found in the folder:

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

| Field | Description |
|---|---|
| `secret_name` | Name of the secret in Secret Server |
| `target_host` | Value of the `machine` field — the host that was connected to |
| `ssh_hostname` | Output of `hostname` on the remote server; `null` on failure |
| `ip_address` | Primary IP from `hostname -I`; `null` on failure |
| `status` | `success` or `failed` |
| `error` | Error message on failure; `null` on success |

A single unreachable server does **not** abort the run.

---

## Troubleshooting

### Secret Server / authentication errors (abort the whole run)

**`ST2 KV key 'ss_username' not found or empty`**
: Set the key: `st2 key set ss_username "AnsibleAPI"`

**`ST2 KV key 'ss_password' not found or empty`**
: Set the key: `st2 key set ss_password "<password>" --encrypt`

**`Secret Server authentication failed [400]`**
: Username or password is wrong. Verify with:
  `python3 list_folder_secrets.py --base-url <url> --username <user> --password <pass> --folder-id 617`

**`Secret Server authentication failed [4xx]`**
: Check that `ss_url` points to the correct Secret Server instance and that the
  OAuth2 endpoint `{ss_url}/oauth2/token` is reachable from the ST2 runner.

**`Folder 'X' not found in Secret Server`**
: The folder name does not exist or the SDK client does not have permission to view
  it. Verify the folder name and check the client's role assignments in Secret Server.

---

### Per-host SSH errors (captured in result, run continues)

| Error | Cause |
|---|---|
| `Authentication failed for user 'root'` | Wrong or expired password in Secret Server |
| `DNS resolution failed for 'host'` | FQDN in the `machine` field cannot be resolved |
| `Connection timed out after 10s` | Host unreachable on SSH port; increase `ssh_timeout` or check firewall |
| `Connection refused on host:22` | SSH not running or on a different port |
| `SSH handshake/banner error` | TCP connected but SSH negotiation failed |
| `Remote command timed out` | Host connected but command did not complete; increase `ssh_timeout` |
| `Command returned no output` | Command ran but stdout was empty; stderr hint included |
| `Secret is missing the 'machine' field` | Secret exists but has no FQDN/IP stored |
| `Secret is missing username or password field` | One or both credential fields are blank |
