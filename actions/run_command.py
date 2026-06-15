import socket

import paramiko
import paramiko.ssh_exception
import requests
from st2common.runners.base_action import Action

_PAGE_SIZE = 100
_AUTH_FAIL_SENTINEL = "AUTH_FAILED"


class RunCommandAction(Action):
    """
    For each host in the inventory:
      1. Derive the short name (part before the first '.').
      2. Search Secret Server for '<short>_root'; attempt SSH + run command.
      3. On auth failure or missing secret, fall back to '<short>_sysadmin'.
      4. Non-auth connection errors (timeout, DNS, refused) fail immediately —
         retrying with different credentials won't help.
    """

    def run(
        self,
        hosts,
        command,
        ss_kv_username,
        ss_kv_password,
        ss_kv_url_key,
        ssh_port,
        ssh_timeout,
    ):
        # ── Read config from ST2 KV ────────────────────────────────────────
        ss_username = (
            self.action_service.get_value(ss_kv_username, decrypt=False, local=False) or ""
        ).strip()
        ss_password = (
            self.action_service.get_value(ss_kv_password, decrypt=True, local=False) or ""
        ).strip()
        ss_url = (
            self.action_service.get_value(ss_kv_url_key, decrypt=False, local=False) or ""
        ).strip().rstrip("/")

        if not ss_username:
            raise Exception(f"ST2 KV key '{ss_kv_username}' not found or empty.")
        if not ss_password:
            raise Exception(f"ST2 KV key '{ss_kv_password}' not found or empty.")
        if not ss_url:
            raise Exception(f"ST2 KV key '{ss_kv_url_key}' not found or empty.")

        ss_token = self._get_token(ss_url, ss_username, ss_password)

        results = []
        for host in hosts:
            host = host.strip()
            if not host:
                continue
            result = self._run_on_host(ss_url, ss_token, host, command, ssh_port, ssh_timeout)
            self.logger.info(
                f"{host}: status={result['status']}  user={result.get('user') or '-'}  "
                f"error={result.get('error') or '-'}"
            )
            results.append(result)

        self.logger.info("\n" + self._format_table(results, command))
        return results

    # ------------------------------------------------------------------
    # Per-host execution
    # ------------------------------------------------------------------

    def _run_on_host(self, ss_url, ss_token, host, command, ssh_port, ssh_timeout):
        short = host.split(".")[0]

        for suffix in ("_root", "_sysadmin"):
            secret_name = f"{short}{suffix}"
            secret = self._find_secret_by_name(ss_url, ss_token, secret_name)

            if secret is None:
                self.logger.info(f"Secret '{secret_name}' not found in Secret Server.")
                continue

            ssh_user = self._get_field_value(secret, ["username", "user", "login"])
            ssh_pass = self._get_field_value(secret, ["password", "pass", "pw"])

            if not ssh_user or not ssh_pass:
                self.logger.warning(f"Secret '{secret_name}' is missing username or password field.")
                continue

            output, error, retry = self._ssh_run(
                host, ssh_user, ssh_pass, command, ssh_port, ssh_timeout
            )

            if retry:
                self.logger.info(
                    f"Auth failed using '{secret_name}' on {host}; trying next credential."
                )
                continue

            return {
                "host": host,
                "user": ssh_user,
                "secret_used": secret_name,
                "command_output": output,
                "status": "success" if not error else "failed",
                "error": error,
            }

        return {
            "host": host,
            "user": None,
            "secret_used": None,
            "command_output": None,
            "status": "failed",
            "error": (
                f"No usable credentials found for '{host}' "
                f"(tried '{short}_root' and '{short}_sysadmin')."
            ),
        }

    def _ssh_run(self, host, username, password, command, port, timeout):
        """
        Connect via SSH and run `command`.
        Returns (output, error, retry) where retry=True signals an auth failure
        so the caller can attempt the next credential without giving up on the host.
        """
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            try:
                ssh.connect(
                    host,
                    port=port,
                    username=username,
                    password=password,
                    timeout=timeout,
                    look_for_keys=False,
                    allow_agent=False,
                )
            except paramiko.AuthenticationException:
                return None, f"Authentication failed for user '{username}'.", True
            except paramiko.ssh_exception.NoValidConnectionsError as exc:
                return None, f"Could not open SSH connection to {host}:{port}: {exc}", False
            except paramiko.SSHException as exc:
                return None, f"SSH handshake error on {host}:{port}: {exc}", False
            except socket.gaierror as exc:
                return None, f"DNS resolution failed for '{host}': {exc.strerror}", False
            except socket.timeout:
                return None, f"Connection timed out after {timeout}s — {host}:{port} unreachable.", False
            except ConnectionRefusedError:
                return None, f"Connection refused — nothing listening on {host}:{port}.", False
            except OSError as exc:
                return None, f"Network error connecting to {host}:{port}: {exc}", False

            try:
                _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
                output = stdout.read().decode().strip()
                stderr_text = stderr.read().decode().strip()
            except socket.timeout:
                return None, f"Remote command timed out after {timeout}s.", False
            except paramiko.SSHException as exc:
                return None, f"Failed to execute remote command: {exc}", False

            if not output and stderr_text:
                output = stderr_text
            return output or "(no output)", None, False

        finally:
            ssh.close()

    # ------------------------------------------------------------------
    # Secret Server helpers
    # ------------------------------------------------------------------

    def _get_token(self, ss_url, username, password):
        token_url = f"{ss_url}/oauth2/token"
        try:
            response = requests.post(
                token_url,
                data={"grant_type": "password", "username": username, "password": password},
                timeout=30,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise Exception(
                f"Secret Server authentication failed [{exc.response.status_code}]: "
                f"{exc.response.text.strip()}"
            )
        except requests.RequestException as exc:
            raise Exception(f"Could not reach Secret Server at {token_url}: {exc}")

        token = response.json().get("access_token")
        if not token:
            raise Exception(f"OAuth2 response missing access_token: {response.text.strip()}")
        return token

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

    def _find_secret_by_name(self, ss_url, ss_token, secret_name):
        """Search Secret Server for an exact name match; return the full secret or None."""
        data = self._api_get(
            ss_url, ss_token, "/api/v1/secrets",
            {"filter.searchText": secret_name, "take": 10},
        )
        for record in data.get("records", []):
            if (record.get("name") or "").strip().lower() == secret_name.lower():
                secret_id = record["id"]
                return self._api_get(ss_url, ss_token, f"/api/v1/secrets/{secret_id}")
        return None

    # ------------------------------------------------------------------
    # Output formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_table(results, command):
        _MAX_OUT = 80
        _MAX_ERR = 60
        headers = ["Host", "User", "Secret Used", "Command Output", "Status", "Error"]
        rows = [
            [
                r.get("host") or "-",
                r.get("user") or "-",
                r.get("secret_used") or "-",
                (r.get("command_output") or "")[:_MAX_OUT].replace("\n", " | "),
                r.get("status") or "-",
                (r.get("error") or "")[:_MAX_ERR],
            ]
            for r in results
        ]

        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(str(cell)))

        sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

        def fmt(cells):
            return "|" + "|".join(f" {str(c):<{w}} " for c, w in zip(cells, widths)) + "|"

        lines = [f"  Command: {command}", sep, fmt(headers), sep]
        lines += [fmt(r) for r in rows]
        lines += [sep]

        success = sum(1 for r in results if r.get("status") == "success")
        failed = len(results) - success
        lines.append(f"  Total: {len(results)}  |  Success: {success}  |  Failed: {failed}")

        return "\n".join(lines)

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
