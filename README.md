# server_audit Pack

Enumerates every secret inside a Delinea Secret Server folder and SSH-probes each
server to verify connectivity and collect the live hostname and OS version.

Each secret is expected to follow the **Azure-Linux-Root** template structure:

| Secret field | Slug | Purpose |
|---|---|---|
| Machine | `machine` | FQDN or IP used as the SSH target |
| Username | `username` | SSH login username |
| Password | `password` | SSH login password |

The Secret Server SDK token and base URL are never hard-coded — they are read
at runtime from the StackStorm KV store.

---

## Prerequisites

| Requirement | Minimum version |
|---|---|
| StackStorm | 3.8+ |
| Python | 3.8 – 3.11 |
| Delinea Secret Server | Any version exposing the v1 REST API |

Network access is required from the StackStorm sensor/action runner to:
- The Secret Server HTTPS endpoint
- Port 22 (or custom `ssh_port`) on every target server in the folder

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

This installs the libraries listed in `requirements.txt` into an isolated
virtualenv for the pack:

```
delinea-secret-server-python-sdk
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

Expected output:

```
+-----------------------------+--------------------------------------------------------------------+
| ref                         | description                                                        |
+-----------------------------+--------------------------------------------------------------------+
| server_audit.audit_servers  | List every secret in a Secret Server folder, SSH into each server  |
|                             | using the credentials stored in the secret (machine / username /   |
|                             | password fields), and return the live hostname and OS version.     |
+-----------------------------+--------------------------------------------------------------------+
```

---

## Configuration

No pack config file is required. All secrets are stored in the StackStorm KV store.

### Store the Secret Server SDK token (encrypted)

```bash
st2 key set ss_sdk_token "<your-delinea-sdk-token>" --encrypt
```

### Store the Secret Server base URL (plain text)

```bash
st2 key set ss_url "https://secretserver.example.com"
```

> **Custom key names** — if you need multiple Secret Server instances or prefer
> different key names, pass `ss_kv_token_key` and `ss_kv_url_key` at run time
> (see [Parameters](#parameters) below).

### Verify the KV entries

```bash
st2 key list
```

You should see both keys:

```
+---------------+-----------+--------+
| name          | scope     | secret |
+---------------+-----------+--------+
| ss_sdk_token  | system    | True   |
| ss_url        | system    | False  |
+---------------+-----------+--------+
```

---

## Usage

### Run against a folder (all defaults)

```bash
st2 run server_audit.audit_servers folder_name="Linux Servers"
```

### Run with a custom SSH port and timeout

```bash
st2 run server_audit.audit_servers \
  folder_name="Linux Servers" \
  ssh_port=2222 \
  ssh_timeout=20
```

### Run with non-default KV key names

```bash
st2 run server_audit.audit_servers \
  folder_name="Linux Servers" \
  ss_kv_token_key=prod_ss_token \
  ss_kv_url_key=prod_ss_url
```

---

## Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `folder_name` | string | yes | — | Name of the Secret Server folder to enumerate |
| `ss_kv_token_key` | string | no | `ss_sdk_token` | ST2 KV key holding the Secret Server SDK token (stored encrypted) |
| `ss_kv_url_key` | string | no | `ss_url` | ST2 KV key holding the Secret Server base URL |
| `ssh_port` | integer | no | `22` | SSH port to connect on |
| `ssh_timeout` | integer | no | `10` | SSH connection timeout in seconds |

---

## Output

The action returns a list — one entry per secret found in the folder.

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
    "error": "SSH authentication failed."
  }
]
```

| Field | Description |
|---|---|
| `secret_name` | Name of the secret in Secret Server (used as an identifier) |
| `target_host` | Value of the `machine` field — the host that was connected to |
| `ssh_hostname` | Output of `hostname` on the remote server; `null` on failure |
| `os_version` | `PRETTY_NAME` from `/etc/os-release`, or `uname -sr` fallback; `null` on failure |
| `status` | `success` or `failed` |
| `error` | Error message on failure; `null` on success |

A single unreachable server does **not** abort the run — the error is captured
in that entry and the action continues with the remaining servers.

---

## Troubleshooting

Individual host failures are captured per-entry in the results list and do **not**
abort the run. The `error` field contains one of the messages below.

---

### Secret Server errors (affect the whole run)

**`ST2 KV key 'ss_sdk_token' not found or empty`**
: The KV key is missing or was stored without `--encrypt`. Re-set it:
  ```bash
  st2 key set ss_sdk_token "<token>" --encrypt
  ```

**`Folder 'X' not found in Secret Server`**
: The folder name must match exactly (case-insensitive). Confirm the name in the
  Secret Server UI and ensure the SDK token has read access to that folder.

---

### Per-host errors (captured in the result, run continues)

**`Authentication failed for user 'root' — check the password in Secret Server`**
: The stored password is wrong or has expired. In the Secret Server UI the secret's
  `lastHeartBeatStatus` will show `Failed` when this happens.

**`DNS resolution failed for 'hostname.example.com': Name or service not known`**
: The FQDN in the `machine` field cannot be resolved. Verify the value in the
  secret and that the ST2 action runner's DNS can reach the target domain.

**`Connection timed out after 10s — hostname.example.com:22 is unreachable or too slow`**
: The host is not reachable on the SSH port within the timeout window. Check
  firewall rules and increase `ssh_timeout` if the host is genuinely slow to respond.

**`Connection refused — nothing is listening on hostname.example.com:22`**
: SSH is not running on the target or is on a non-standard port. Verify the service
  is up, or pass a custom `ssh_port` if it differs from 22.

**`Could not open any SSH connections to hostname.example.com:22`**
: Paramiko exhausted all connection attempts. This usually means an intermediate
  network device (load balancer, proxy) is dropping the packets silently.

**`SSH handshake/banner error connecting to hostname.example.com:22`**
: The TCP connection succeeded but SSH negotiation failed — the remote may not be
  an SSH server, or it rejected the client's algorithms. Check the sshd config on
  the target.

**`Network error connecting to hostname.example.com:22`**
: A general OS-level error (e.g. no route to host, network unreachable). The full
  OS error string is included in the message for diagnosis.

**`Remote command timed out after 10s`**
: Connected successfully but the `hostname` / `os-release` command did not complete
  within the timeout. The host may be under extreme load. Increase `ssh_timeout`.

**`Failed to execute remote command`**
: The SSH channel was lost after the connection was established. Usually indicates
  the sshd process on the target crashed or the session was forcibly closed.

**`Command returned no output`**
: The remote command ran but produced nothing on stdout. Any stderr output is
  appended to the error message to help diagnose the cause.

**`Secret is missing the 'machine' field`**
: The secret exists in Secret Server but has no `machine` slug/field. Update the
  secret in the UI to add the FQDN or IP address.

**`Secret is missing username or password field`**
: One or both credential fields are blank in Secret Server. Update the secret.
