import argparse
import json
import os
import sys
from typing import Any, Dict, Optional

from delinea.secrets.server import (
    AccessTokenAuthorizer,
    DomainPasswordGrantAuthorizer,
    PasswordGrantAuthorizer,
    SecretServer,
    SecretServerError,
    ServerSecret,
)


def _env(name: str) -> Optional[str]:
    value = os.getenv(name)
    return value if value not in {None, ""} else None


def _env_int(name: str) -> Optional[int]:
    raw = _env(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch a secret from Delinea Secret Server using the python-tss-sdk."
    )
    parser.add_argument(
        "--base-url",
        default=_env("TSS_BASE_URL"),
        help="Secret Server base URL (env: TSS_BASE_URL)",
    )

    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--secret-id",
        type=int,
        default=_env_int("TSS_SECRET_ID"),
        help="Numeric Secret ID to retrieve (env: TSS_SECRET_ID)",
    )
    target_group.add_argument(
        "--secret-path",
        default=_env("TSS_SECRET_PATH"),
        help="Full secret path, e.g. 'Folder/Sub/Secret' (env: TSS_SECRET_PATH)",
    )

    credential_group = parser.add_mutually_exclusive_group()
    credential_group.add_argument(
        "--access-token",
        default=_env("TSS_ACCESS_TOKEN"),
        help="Pre-issued OAuth access token (env: TSS_ACCESS_TOKEN)",
    )

    password_group = credential_group.add_argument_group("password grant options")
    password_group.add_argument(
        "--username",
        default=_env("TSS_USERNAME"),
        help="Secret Server username (env: TSS_USERNAME)",
    )
    password_group.add_argument(
        "--password",
        default=_env("TSS_PASSWORD"),
        help="Secret Server password (env: TSS_PASSWORD)",
    )
    password_group.add_argument(
        "--domain",
        default=_env("TSS_DOMAIN"),
        help="Active Directory domain for domain logins (env: TSS_DOMAIN)",
    )

    parser.add_argument(
        "--field",
        default=_env("TSS_SECRET_FIELD"),
        help="Field slug to print instead of whole secret JSON (env: TSS_SECRET_FIELD)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output when not selecting a single field.",
    )

    return parser


def ensure_required_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if not args.base_url:
        parser.error("--base-url or TSS_BASE_URL environment variable is required")

    if args.secret_id is None and not args.secret_path:
        parser.error("Provide --secret-id or --secret-path (or matching environment variables)")


def create_authorizer(args: argparse.Namespace) -> Any:
    if args.access_token:
        return AccessTokenAuthorizer(args.access_token, args.base_url)

    if args.username or args.password or args.domain:
        if not args.username or not args.password:
            raise SystemExit("--username and --password are required for password authorization")
        if args.domain:
            return DomainPasswordGrantAuthorizer(
                args.base_url,
                args.username,
                args.domain,
                args.password,
            )
        return PasswordGrantAuthorizer(args.base_url, args.username, args.password)

    raise SystemExit("Provide credentials via --access-token or --username/--password")


def fetch_secret(
    client: SecretServer,
    *,
    secret_id: Optional[int],
    secret_path: Optional[str],
) -> Dict[str, Any]:
    if secret_id is not None:
        return client.get_secret(secret_id)
    assert secret_path is not None
    return client.get_secret_by_path(secret_path)


def output_secret(secret: Dict[str, Any], field_slug: Optional[str], pretty: bool) -> None:
    if field_slug:
        server_secret = ServerSecret(**secret)
        field = server_secret.fields.get(field_slug)
        if field is None:
            available = ", ".join(sorted(server_secret.fields)) or "<none>"
            raise SystemExit(
                f"Secret does not contain a field with slug '{field_slug}'. Available: {available}"
            )
        print(field.value)
        return

    if pretty:
        print(json.dumps(secret, indent=2))
    else:
        print(json.dumps(secret))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    ensure_required_args(args, parser)

    try:
        authorizer = create_authorizer(args)
        client = SecretServer(args.base_url, authorizer=authorizer)
        secret = fetch_secret(client, secret_id=args.secret_id, secret_path=args.secret_path)
        output_secret(secret, args.field, args.pretty)
    except SecretServerError as exc:
        print(f"Secret Server error: {exc.message}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # pragma: no cover - defensive catch for CLI usage
        print(f"Failed to retrieve secret: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()