import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from st2common.runners.base_action import Action


class PingCheckAction(Action):
    """
    For each host in the comma-separated inventory:
      - DNS lookup via socket.gethostbyname_ex (all returned IPs)
      - Ping test via system ping binary (packet loss + avg RTT)
    Hosts are checked in parallel (max_workers controls concurrency).
    Both checks run regardless of each other; failures are captured per-host.
    """

    def run(self, hosts, ping_count, ping_timeout, max_workers):
        host_list = [h.strip() for h in hosts.split(",") if h.strip()]
        self.logger.info(
            f"Checking {len(host_list)} hosts with {max_workers} parallel workers "
            f"(ping_count={ping_count}, ping_timeout={ping_timeout}s)"
        )

        results_map = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._check_host, host, ping_count, ping_timeout): host
                for host in host_list
            }
            for future in as_completed(futures):
                host = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = self._error_result(host, str(exc))
                self.logger.info(
                    f"{host}: dns={result['dns_status']}  ping={result['ping_status']}  "
                    f"ips={result['ip_addresses']}  loss={result['packet_loss']}  "
                    f"rtt={result['avg_rtt_ms']}"
                )
                results_map[host] = result

        # Preserve original input order
        results = [results_map[h] for h in host_list if h in results_map]
        self.logger.info("\n" + self._format_table(results))
        return results

    # ------------------------------------------------------------------
    # Per-host checks
    # ------------------------------------------------------------------

    def _check_host(self, host, ping_count, ping_timeout):
        ip_addresses, dns_status, dns_error = self._dns_lookup(host)
        ping_status, packet_loss, avg_rtt, ping_error = self._ping(host, ping_count, ping_timeout)

        errors = [e for e in (dns_error, ping_error) if e]
        return {
            "host": host,
            "ip_addresses": ", ".join(ip_addresses) if ip_addresses else "-",
            "dns_status": dns_status,
            "ping_status": ping_status,
            "packet_loss": packet_loss,
            "avg_rtt_ms": avg_rtt,
            "error": " | ".join(errors) if errors else None,
        }

    @staticmethod
    def _error_result(host, error):
        return {
            "host": host,
            "ip_addresses": "-",
            "dns_status": "error",
            "ping_status": "error",
            "packet_loss": "N/A",
            "avg_rtt_ms": "N/A",
            "error": error,
        }

    def _dns_lookup(self, host):
        try:
            _, _, ip_list = socket.gethostbyname_ex(host)
            return ip_list, "resolved", None
        except socket.gaierror as exc:
            return [], "failed", f"DNS: {exc.strerror}"

    def _ping(self, host, count, timeout):
        try:
            proc = subprocess.run(
                ["ping", "-c", str(count), "-W", str(timeout), host],
                capture_output=True,
                text=True,
                timeout=count * timeout + 5,
            )
            output = proc.stdout + proc.stderr

            loss_match = re.search(r"(\d+(?:\.\d+)?)% packet loss", output)
            packet_loss = (loss_match.group(1) + "%") if loss_match else "N/A"

            # "rtt min/avg/max/mdev = 0.1/0.2/0.3/0.4 ms"
            rtt_match = re.search(r"min/avg/max[^=]*=\s*[\d.]+/([\d.]+)/", output)
            avg_rtt = (rtt_match.group(1) + " ms") if rtt_match else "N/A"

            if proc.returncode == 0:
                return "reachable", packet_loss, avg_rtt, None
            else:
                return "unreachable", packet_loss, avg_rtt, "Ping: host did not respond"

        except subprocess.TimeoutExpired:
            return "unreachable", "N/A", "N/A", f"Ping: timed out"
        except FileNotFoundError:
            return "error", "N/A", "N/A", "ping binary not found on this system"
        except Exception as exc:
            return "error", "N/A", "N/A", f"Ping error: {exc}"

    # ------------------------------------------------------------------
    # Output formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_table(results):
        _MAX_IP = 40
        _MAX_ERR = 55
        headers = ["Host", "IP Address(es)", "DNS", "Ping", "Packet Loss", "Avg RTT", "Error"]
        rows = [
            [
                r.get("host") or "-",
                (r.get("ip_addresses") or "-")[:_MAX_IP],
                r.get("dns_status") or "-",
                r.get("ping_status") or "-",
                r.get("packet_loss") or "-",
                r.get("avg_rtt_ms") or "-",
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

        lines = [sep, fmt(headers), sep] + [fmt(r) for r in rows] + [sep]

        reachable = sum(1 for r in results if r.get("ping_status") == "reachable")
        resolved  = sum(1 for r in results if r.get("dns_status") == "resolved")
        total     = len(results)
        lines.append(
            f"  Total: {total}  |  DNS resolved: {resolved}  |  Ping reachable: {reachable}"
        )

        return "\n".join(lines)
