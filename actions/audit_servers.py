import socket

import paramiko
import requests
from st2common.runners.base_action import Action

_PAGE_SIZE = 100


class AuditServersAction(Action):
    """
    1. Read username, password, and URL from ST2 KV.
    2. Obtain a Bearer token via password grant (POST /oauth2/token).
    3. Find the requested folder and list every secret it contains.
    4. For each secret: SSH to the 'machine' field host, run hostname + IP lookup,
       and return the result. Unreachable hosts are captured as failures and do not
       abort the run — the machine field from the secret is always included.
    """

    def run(
        self,
        folder_name,
        ss_kv_username,
        ss_kv_password,
        ss_kv_url_key,
        ssh_port,
        ssh_timeout,
    ):
        # ── Read config from ST2 KV ────────────────────────────────────────
        username = (
            self.action_service.get_value(ss_kv_username, decrypt=False, local=False) or ""
        ).strip()
        password = (
            self.action_service.get_value(ss_kv_password, decrypt=True, local=False) or ""
        ).strip()
        ss_url = (
            self.action_service.get_value(ss_kv_url_key, decrypt=False, local=False) or ""
        ).strip().rstrip("/")

        if not username:
            raise Exception(f"ST2 KV key '{ss_kv_username}' not found or empty.")
        if not password:
            raise Exception(f"ST2 KV key '{ss_kv_password}' not found or empty.")
        if not ss_url:
            raise Exception(f"ST2 KV key '{ss_kv_url_key}' not found or empty.")

        # ── Obtain Bearer token ────────────────────────────────────────────
        ss_token = self._get_token(ss_url, username, password)

        # ── Enumerate folder and probe each server ─────────────────────────
        folder_id = self._resolve_folder_id(ss_url, ss_token, folder_name)
        self.logger.info(f"Resolved folder '{folder_name}' to id={folder_id}")

        secrets = self._list_folder_secrets(ss_url, ss_token, folder_id)
        self.logger.info(f"Found {len(secrets)} secret(s) in folder '{folder_name}'")

        if not secrets:
            return []

        results = []
        for secret_info in secrets:
            secret_name = (secret_info.get("name") or "").strip()
            secret_id = secret_info.get("id")
            if not secret_name or not secret_id:
                self.logger.warning(f"Skipping entry with missing name or id: {secret_info}")
                continue
            result = self._audit_server(
                ss_url, ss_token, secret_id, secret_name, ssh_port, ssh_timeout
            )
            self.logger.info(
                f"{secret_name} ({result.get('target_host')}): "
                f"status={result['status']}  "
                f"ssh_hostname={result.get('ssh_hostname')}  "
                f"ip_address={result.get('ip_address')}  "
                f"error={result.get('error')}"
            )
            results.append(result)

        return results

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _get_token(self, ss_url, username, password):
        """Obtain a Bearer token via username/password grant."""
        token_url = f"{ss_url}/oauth2/token"
        try:
            response = requests.post(
                token_url,
                data={
                    "grant_type": "password",
                    "username": username,
                    "password": password,
                },
                timeout=30,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code
            body = exc.response.text.strip()
            raise Exception(
                f"Secret Server authentication failed [{status}]: {body}. "
                f"Check ss_username / ss_password in ST2 KV."
            )
        except requests.RequestException as exc:
            raise Exception(
                f"Could not reach Secret Server OAuth2 endpoint {token_url}: {exc}"
            )

        access_token = response.json().get("access_token")
        if not access_token:
            raise Exception(
                f"OAuth2 response did not contain an access_token: {response.text.strip()}"
            )
        return access_token

    # ------------------------------------------------------------------
    # Secret Server REST helpers
    # ------------------------------------------------------------------

    def _api_get(self, ss_url, ss_token, path, params=None):
        headers = {"Authorization": f"Bearer {ss_token}"}
        url = f"{ss_url}{path}"
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            raise Exception(
                f"Secret Server API error [{exc.response.status_code}] for {path}: {exc}"
            )
        except requests.RequestException as exc:
            raise Exception(f"Failed to reach Secret Server at {url}: {exc}")

    def _resolve_folder_id(self, ss_url, ss_token, folder_name):
        data = self._api_get(
            ss_url, ss_token, "/api/v1/folders",
            {"filter.searchText": folder_name, "take": 50},
        )
        records = data.get("records", [])
        folder_name_lower = folder_name.strip().lower()

        for record in records:
            if (record.get("folderName") or "").strip().lower() == folder_name_lower:
                return record["id"]

        if records:
            self.logger.warning(
                f"No exact match for folder '{folder_name}'; "
                f"using '{records[0].get('folderName')}' (id={records[0]['id']})."
            )
            return records[0]["id"]

        raise Exception(f"Folder '{folder_name}' not found in Secret Server.")

    def _list_folder_secrets(self, ss_url, ss_token, folder_id):
        all_records = []
        skip = 0
        while True:
            data = self._api_get(
                ss_url, ss_token, "/api/v1/secrets",
                {
                    "filter.folderId": folder_id,
                    "filter.includeSubFolders": False,
                    "take": _PAGE_SIZE,
                    "skip": skip,
                },
            )
            page = data.get("records", [])
            all_records.extend(page)
            if len(all_records) >= data.get("total", len(all_records)) or not page:
                break
            skip += _PAGE_SIZE
        return all_records

    def _get_secret(self, ss_url, ss_token, secret_id):
        return self._api_get(ss_url, ss_token, f"/api/v1/secrets/{secret_id}")

    # ------------------------------------------------------------------
    # Per-server audit
    # ------------------------------------------------------------------

    def _audit_server(self, ss_url, ss_token, secret_id, secret_name, ssh_port, ssh_timeout):
        try:
            secret = self._get_secret(ss_url, ss_token, secret_id)
        except Exception as exc:
            return self._result(secret_name, None, None, None, f"Secret fetch failed: {exc}")

        target_host = (
            self._get_field_value(secret, ["machine", "host", "server"]) or ""
        ).strip()
        username = self._get_field_value(secret, ["username", "user", "login"])
        password = self._get_field_value(secret, ["password", "pass", "pw"])

        if not target_host:
            return self._result(secret_name, None, None, None, "Secret is missing the 'machine' field.")
        if not username or not password:
            return self._result(
                secret_name, target_host, None, None,
                "Secret is missing username or password field.",
            )

        return self._ssh_probe(secret_name, target_host, username, password, ssh_port, ssh_timeout)

    # Line 0: hostname
    # Line 1: space-separated IP addresses (hostname -I), falling back to
    #         hostname -i (resolves via DNS) on older systems that lack -I.
    _PROBE_CMD = (
        "hostname; "
        "hostname -I 2>/dev/null || hostname -i 2>/dev/null || echo N/A"
    )

    def _ssh_probe(self, secret_name, target_host, username, password, port, timeout):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            # ── Phase 1: establish the connection ──────────────────────────
            try:
                ssh.connect(
                    target_host,
                    port=port,
                    username=username,
                    password=password,
                    timeout=timeout,
                    look_for_keys=False,
                    allow_agent=False,
                )
            except paramiko.AuthenticationException:
                return self._result(
                    secret_name, target_host, None, None,
                    f"Authentication failed for user '{username}' — check the password in Secret Server.",
                )
            except paramiko.NoValidConnectionsError as exc:
                return self._result(
                    secret_name, target_host, None, None,
                    f"Could not open any SSH connections to {target_host}:{port}: {exc}",
                )
            except paramiko.SSHException as exc:
                return self._result(
                    secret_name, target_host, None, None,
                    f"SSH handshake/banner error connecting to {target_host}:{port}: {exc}",
                )
            except socket.gaierror as exc:
                return self._result(
                    secret_name, target_host, None, None,
                    f"DNS resolution failed for '{target_host}': {exc.strerror}",
                )
            except socket.timeout:
                return self._result(
                    secret_name, target_host, None, None,
                    f"Connection timed out after {timeout}s — {target_host}:{port} is unreachable or too slow.",
                )
            except ConnectionRefusedError:
                return self._result(
                    secret_name, target_host, None, None,
                    f"Connection refused — nothing is listening on {target_host}:{port}.",
                )
            except OSError as exc:
                return self._result(
                    secret_name, target_host, None, None,
                    f"Network error connecting to {target_host}:{port}: {exc}",
                )

            # ── Phase 2: run the probe command ─────────────────────────────
            try:
                _, stdout, stderr = ssh.exec_command(self._PROBE_CMD, timeout=timeout)
                output = stdout.read().decode()
                stderr_text = stderr.read().decode().strip()
            except socket.timeout:
                return self._result(
                    secret_name, target_host, None, None,
                    f"Remote command timed out after {timeout}s.",
                )
            except paramiko.SSHException as exc:
                return self._result(
                    secret_name, target_host, None, None,
                    f"Failed to execute remote command: {exc}",
                )

            lines = output.splitlines()
            ssh_hostname = lines[0].strip() if len(lines) > 0 else ""
            # Take the first IP from the space-separated list returned by hostname -I.
            ip_address = (lines[1].split()[0] if len(lines) > 1 and lines[1].strip() else "")

            if not ssh_hostname:
                detail = f" stderr: {stderr_text}" if stderr_text else ""
                return self._result(
                    secret_name, target_host, None, None,
                    f"Command returned no output.{detail}",
                )

            return self._result(
                secret_name, target_host, ssh_hostname, ip_address, None, status="success"
            )

        finally:
            ssh.close()

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _result(secret_name, target_host, ssh_hostname, ip_address, error, status=None):
        if status is None:
            status = "failed" if error else "success"
        return {
            "secret_name": secret_name,
            "target_host": target_host,   # machine field from the secret — always present
            "ssh_hostname": ssh_hostname,
            "ip_address": ip_address,
            "status": status,
            "error": error,
        }

    @staticmethod
    def _get_field_value(secret, candidate_slugs):
        items = secret.get("items", [])
        lowered = [s.lower() for s in candidate_slugs]
        for item in items:
            slug = (item.get("slug") or "").lower()
            field_name = (item.get("fieldName") or "").lower()
            if slug in lowered or field_name in lowered:
                value = item.get("itemValue")
                if value is None:
                    value = item.get("value")
                return value
        return None
