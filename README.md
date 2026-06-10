# server_audit Pack

Enumerates every secret inside a Delinea Secret Server folder and SSH-probes each
server to verify connectivity and collect the live hostname and OS version.

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

The pack uses the **Delinea SDK Client onboarding flow** — no `tss` CLI required.

**First run:**
1. The action reads the **onboarding key** and **rule name** from ST2 KV.
2. It calls `POST /api/v1/sdk-client-accounts` on Secret Server with those credentials. Secret Server validates the runner's IP address against the Client Onboarding Rule and returns a `clientId` and `clientSecret`.
3. Those credentials are written back into ST2 KV (`ss_client_id` and `ss_client_secret`) so the registration endpoint is never called again.
4. The action immediately exchanges the credentials for a fresh OAuth2 Bearer token via `POST /oauth2/token` and uses it for all API calls.

**Subsequent runs:**
- Steps 1–3 are skipped. The action reads the cached `ss_client_id` / `ss_client_secret` from KV, gets a fresh token, and proceeds directly to folder enumeration.

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

Dependencies installed (`requirements.txt`):

```
paramiko
requests
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
# Onboarding key from the SDK Client Management page → "Show Key" (encrypted)
st2 key set ss_sdk_token "<Base64-onboarding-key>" --encrypt

# SDK Client Onboarding Rule name (plain text)
st2 key set ss_rule_name "MyOnboardingRule"

# Secret Server base URL (plain text)
st2 key set ss_url "https://wesco.secretservercloud.com"
```

The following two keys are **written automatically by the action on first run**.
Do not set them manually unless recovering from a failed registration:

```bash
# st2 key set ss_client_id "<client_id>"
# st2 key set ss_client_secret "<client_secret>" --encrypt
```

### Verify KV entries

```bash
st2 key list
```

Expected output after setup:

```
+----------------+---------+--------+
| name           | scope   | secret |
+----------------+---------+--------+
| ss_sdk_token   | system  | True   |
| ss_rule_name   | system  | False  |
| ss_url         | system  | False  |
+----------------+---------+--------+
```

After the first successful run, `ss_client_id` and `ss_client_secret` will also appear.

---

## Usage

### Run against a folder (all defaults)

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

### Override the SDK client name used at registration

```bash
st2 run server_audit.audit_servers \
  folder_name="Azure-Linux" \
  ss_sdk_client_name="st2-automation-prod"
```

### Use non-default KV key names

```bash
st2 run server_audit.audit_servers \
  folder_name="Azure-Linux" \
  ss_kv_onboarding_key=prod_ss_sdk_token \
  ss_kv_rule_name=prod_ss_rule_name \
  ss_kv_url_key=prod_ss_url
```

---

## Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `folder_name` | string | yes | — | Name of the Secret Server folder to enumerate |
| `ss_kv_onboarding_key` | string | no | `ss_sdk_token` | KV key holding the SDK onboarding key (encrypted) |
| `ss_kv_rule_name` | string | no | `ss_rule_name` | KV key holding the SDK Client Onboarding Rule name |
| `ss_kv_url_key` | string | no | `ss_url` | KV key holding the Secret Server base URL |
| `ss_kv_cached_client_id` | string | no | `ss_client_id` | KV key where the action writes the registered client ID |
| `ss_kv_cached_client_secret` | string | no | `ss_client_secret` | KV key where the action writes the registered client secret |
| `ss_sdk_client_name` | string | no | runner hostname | SDK client name to register under (first run only) |
| `ssh_port` | integer | no | `22` | SSH port |
| `ssh_timeout` | integer | no | `10` | Connection timeout in seconds |

---

## KV Key Reference

| KV Key | Encrypted | Set by | Purpose |
|---|---|---|---|
| `ss_sdk_token` | yes | operator | Onboarding key from SDK Client Management |
| `ss_rule_name` | no | operator | SDK Client Onboarding Rule name |
| `ss_url` | no | operator | Secret Server base URL |
| `ss_client_id` | no | action (auto) | Registered SDK client ID |
| `ss_client_secret` | yes | action (auto) | Registered SDK client secret |

---

## Output

A list — one entry per secret found in the folder:

```json
[
  {
    "secret_name": "w2lcslogcl06p_root",
    "target_host": "w2lcslogcl06p.wescodist.com",
    "ssh_hostname": "w2lcslogcl06p",
    "os_version": "Red Hat Enterprise Linux 8.10 (Ootpa)",
    "status": "success",
    "error": null
  },
  {
    "secret_name": "w2lcslogcl07p_root",
    "target_host": "w2lcslogcl07p.wescodist.com",
    "ssh_hostname": null,
    "os_version": null,
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
| `os_version` | `PRETTY_NAME` from `/etc/os-release`; `null` on failure |
| `status` | `success` or `failed` |
| `error` | Error message on failure; `null` on success |

A single unreachable server does **not** abort the run.

---

## Troubleshooting

### Secret Server / registration errors (abort the whole run)

**`ST2 KV key 'ss_sdk_token' not found or empty`**
: Set the key: `st2 key set ss_sdk_token "<key>" --encrypt`

**`ST2 KV key 'ss_rule_name' not found or empty`**
: Set the key: `st2 key set ss_rule_name "MyOnboardingRule"`

**`SDK registration rejected (HTTP 400)`**
: The onboarding key is invalid or has the wrong format. Retrieve a fresh key from
  **Secret Server → Admin → SDK Client Management → Show Key**.

**`SDK registration denied (HTTP 403)`**
: The ST2 runner's IP is not within the CIDR range allowed by the Client Onboarding
  Rule, or the onboarding key has expired. Check the rule in **SDK Client Management**.

**`SDK registration endpoint not found`**
: The registration path is not found at either known location. Open
  `{ss_url}/swagger` and search for `sdk-client-accounts` to find the correct path
  on your Secret Server version.

**`SDK registration conflict (HTTP 409)`**
: A client with the same name already exists. Either:
  1. Delete the existing SDK client in **Secret Server → Admin → SDK Client Management**, then re-run.
  2. Or manually set `ss_client_id` and `ss_client_secret` in ST2 KV with the existing credentials and re-run (the action will use the cache and skip registration).

**`OAuth2 token exchange failed`**
: The cached `ss_client_id` / `ss_client_secret` are no longer valid (the SDK client
  may have been deleted or revoked in Secret Server). Clear the cached keys and re-run
  to trigger re-registration:
  ```bash
  st2 key delete ss_client_id
  st2 key delete ss_client_secret
  ```

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
| `Secret is missing the 'machine' field'` | Secret exists but has no FQDN/IP stored |
| `Secret is missing username or password field` | One or both credential fields are blank |
