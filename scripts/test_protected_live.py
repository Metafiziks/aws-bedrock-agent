#!/usr/bin/env python3
"""Acceptance test for protected mode against a deployed protected API.

Two users, two synthetic documents: a restricted file only user A may read and a control
with no readers and a different fact. Tokens are Cognito ID tokens and are never printed
or saved. Either:

  * prompt (default): paste user A's and user B's ID tokens at hidden prompts (and
    optionally an expired one) - get them from your app's sign-in; or
  * --mint-users: create two synthetic Cognito users in the deployment's pool with
    random passwords generated locally (never printed), mint tokens with admin auth for
    every check, and delete both users at the end. Refuses if a user already exists.
"""

import argparse
import base64
import json
import re
import secrets
import string
import sys
import tempfile
import time
from getpass import getpass
from pathlib import Path

import httpx

NO_DOCUMENTS = "I could not find information about that in the available documents."
CASE_FIELDS = ("allowed_user", "denied_user", "approved_file", "control_file",
               "question", "expected", "control_question", "control_expected")
EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def load_config(path):
    with Path(path).open(encoding="utf-8") as handle:
        config = json.load(handle)
    fields = ("directory", *CASE_FIELDS)
    if not isinstance(config, dict) or set(config) != set(fields) or not all(
        isinstance(v, str) and v.strip() for v in config.values()
    ):
        raise ValueError(f"Acceptance JSON must contain exactly these nonempty strings: {', '.join(fields)}")
    if config["expected"].casefold() == config["control_expected"].casefold():
        raise ValueError("The restricted and control test facts must be different")
    if config["allowed_user"] == config["denied_user"] or config["allowed_user"].casefold() == config["denied_user"].casefold():
        raise ValueError("Use two different test users")
    if not all(EMAIL.fullmatch(config[k]) for k in ("allowed_user", "denied_user")):
        raise ValueError("Test users must be email addresses")
    if config["approved_file"] == config["control_file"]:
        raise ValueError("Use two distinct fixtures")
    return config


def read_token(label):
    import termios

    prompt = f"{label}: "
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    mode = termios.tcgetattr(fd)
    # Canonical input cannot hold a full ID token on macOS.
    mode[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHONL)
    mode[6][termios.VMIN] = 1
    mode[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSAFLUSH, mode)
    try:
        return getpass(prompt).strip()
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, original)


class CognitoUsers:
    """Synthetic users in the deployment's own user pool. Passwords exist only in memory."""

    def __init__(self, cognito, pool_id, client_id):
        self.cognito, self.pool_id, self.client_id = cognito, pool_id, client_id
        self.passwords = {}

    def create(self, email):
        try:
            self.cognito.admin_get_user(UserPoolId=self.pool_id, Username=email)
        except self.cognito.exceptions.UserNotFoundException:
            pass
        else:
            raise RuntimeError("A test user already exists in the pool; refusing to touch an existing user")
        password = "".join(secrets.choice(string.ascii_letters + string.digits + "!#%^*-_") for _ in range(32)) + "aA1!"
        self.cognito.admin_create_user(
            UserPoolId=self.pool_id, Username=email, MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
        )
        self.passwords[email] = password
        self.cognito.admin_set_user_password(UserPoolId=self.pool_id, Username=email, Password=password, Permanent=True)

    def token(self, email):
        result = self.cognito.admin_initiate_auth(
            UserPoolId=self.pool_id, ClientId=self.client_id, AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": self.passwords[email]},
        )
        return result["AuthenticationResult"]["IdToken"]

    def delete_all(self):
        for email in list(self.passwords):
            try:
                self.cognito.admin_delete_user(UserPoolId=self.pool_id, Username=email)
            except Exception as exc:  # noqa: BLE001
                print(f"WARNING: could not delete test user ({type(exc).__name__}); delete it yourself", flush=True)
        self.passwords.clear()


def tamper(token, email):
    """Keep the signature but swap the email claim: only a broken authorizer accepts this."""
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["email"] = email
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{forged}.{signature}"


def resolve(token):
    return token() if callable(token) else token


def run_tests(http, args, allowed, denied, expired=None, ready_timeout=600, settle_timeout=1500, clean_streak=30,
              interval=10, final_checks=12, sleep=time.sleep, direct_retrieve=None, direct_invoke=None,
              allow_root_operator=False):
    """``allowed``/``denied`` are tokens or callables returning a fresh token (ID tokens expire)."""
    url = args.api_url.rstrip("/") + "/ask"

    def invoke(case, token, question="x", *, body=None, extra=None):
        headers = dict(extra or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = body if body is not None else {"input": question, "store": False, "background": False}
        start = time.monotonic()
        response = http.post(url, headers=headers, json=payload)
        try:
            data = response.json()
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        print(f"{case}: HTTP {response.status_code}, {time.monotonic() - start:.2f}s", flush=True)
        return response.status_code, data

    # Newly synced documents can take a few minutes to become searchable.
    deadline = time.monotonic() + ready_timeout
    while True:
        status, data = invoke("approved", resolve(allowed), args.question)
        text = str(data.get("text", ""))
        if status == 200 and args.expected.casefold() in text.casefold():
            break
        if status == 401 or time.monotonic() >= deadline:
            raise RuntimeError(f"The approved request did not return the restricted fact (HTTP {status})")
        time.sleep(20)
    approved_name = args.approved_file.split("/")[-1]
    if approved_name.casefold() not in text.casefold() or approved_name not in data.get("sources", []):
        raise RuntimeError("The approved answer must cite the restricted file")

    def denied_outcome(label):
        """'clean' for a completed response with no documents, 'leak' if the restricted content came back."""
        status, data = invoke(label, resolve(denied), args.question)
        text = str(data.get("text", ""))
        if status == 200 and (args.expected.casefold() in text.casefold() or approved_name in data.get("sources", [])):
            return "leak"
        if status != 200 or data.get("status") != "completed" or text.strip() != NO_DOCUMENTS or data.get("sources"):
            raise RuntimeError("denied: expected a completed response with no available documents")
        return "clean"

    # Do not assume ACL enforcement is instant or consistent after a sync: do not trust one denied check.
    # Require a long unbroken run of clean denials (resetting on any exposure) and report every exposure.
    streak, leaks, started = 0, 0, time.monotonic()
    while streak < clean_streak:
        if time.monotonic() - started > settle_timeout:
            raise RuntimeError(f"denied: access control did not settle ({leaks} exposures in {int(time.monotonic() - started)}s); keep the API off real traffic")
        if denied_outcome("denied (settling)") == "leak":
            leaks, streak = leaks + 1, 0
            print("WARNING: the denied user retrieved the restricted document; access control is still settling", flush=True)
        else:
            streak += 1
        if streak < clean_streak:
            sleep(interval)
    print(f"denied: {clean_streak} consecutive clean checks after {leaks} exposure(s) "
          f"in {int(time.monotonic() - started)}s", flush=True)

    status, data = invoke("control", resolve(allowed), args.control_question)
    text = str(data.get("text", ""))
    if status != 200 or data.get("status") != "completed" or not text.strip():
        raise RuntimeError("control: expected a completed response")
    if any(v.casefold() in text.casefold() for v in (args.control_expected, args.control_file.split("/")[-1])):
        raise RuntimeError("control: the response disclosed the control fact or cited the control file")

    status, data = invoke("browser identity headers and body fields ignored", resolve(denied), args.question, extra={
        "X-Amzn-Oidc-Identity": args.allowed_user, "X-Forwarded-Email": args.allowed_user, "X-User-Email": args.allowed_user,
    })
    if status != 200 or data.get("text", "").strip() != NO_DOCUMENTS:
        raise RuntimeError("Browser-supplied identity headers changed the result")
    for field in ("userContext", "userId", "email"):
        status, data = invoke(f"body field {field}", resolve(denied), body={
            "input": args.question, "store": False, field: {"userId": args.allowed_user} if field == "userContext" else args.allowed_user})
        if status != 400 or data.get("text"):
            raise RuntimeError(f"A body-supplied {field} was not rejected")

    status, data = invoke("missing user token", None, args.question)
    if status not in (401, 403) or data.get("text"):
        raise RuntimeError("Missing user credential was not rejected")
    status, data = invoke("invalid user token", "eyJ.invalid.token-" + "x" * 30, args.question)
    if status not in (401, 403) or data.get("text"):
        raise RuntimeError("An invalid user credential was not rejected")
    status, data = invoke("token with swapped email claim", tamper(resolve(denied), args.allowed_user), args.question)
    if status not in (401, 403) or data.get("text"):
        raise RuntimeError("A token with a forged email claim was not rejected")
    if expired:
        status, data = invoke("expired user token", expired, args.question)
        if status not in (401, 403) or data.get("text"):
            raise RuntimeError("Expired user credential was not rejected")
    for case, body in (
        ("stateful store=true", {"input": args.question, "store": True}),
        ("sessionId", {"input": args.question, "store": False, "sessionId": "s-123456"}),
        ("memoryId", {"input": args.question, "store": False, "memoryId": "m-123456"}),
        ("previous_response_id", {"input": args.question, "store": False, "previous_response_id": "r1"}),
        ("conversation", {"input": args.question, "store": False, "conversation": "c1"}),
    ):
        status, data = invoke(case, resolve(allowed), body=body)
        if status != 400 or data.get("text"):
            raise RuntimeError(f"{case}: stateful input was not rejected")

    # Re-check at the end: a late exposure after the settle phase is a failure.
    for _ in range(final_checks):
        if denied_outcome("denied (final)") == "leak":
            raise RuntimeError("denied: the restricted document was exposed to the denied user after access control had settled")
        sleep(1)

    # The platform trusts userContext, so no other principal may retrieve (knowledge base resource policy).
    if direct_retrieve is not None:
        outcome = direct_retrieve()
        print(f"direct Retrieve as the operator with userContext set: {outcome}", flush=True)
        if outcome == "root-exempt":
            # Observed live: the account root user is NOT blocked by the knowledge base resource policy.
            if not allow_root_operator:
                raise RuntimeError("The operator is the account root user, which the knowledge base resource policy does not "
                                   "restrict, so the lock cannot be verified. Run as an IAM role/user, or pass --allow-root-operator")
            print("WARNING: operator is the root user; the resource-policy lock was NOT verified by this run", flush=True)
        elif outcome != "denied":
            raise RuntimeError("A principal other than the function role could retrieve; the resource policy is not enforced")
    if direct_invoke is not None:
        code = direct_invoke()
        print(f"direct Lambda invoke without validated claims: statusCode {code}", flush=True)
        if code != 401:
            raise RuntimeError("The function answered without validated claims")

    # No credentials of any kind to S3.
    for name in (args.approved_file, args.control_file):
        response = http.head(f"https://{args.bucket}.s3.{args.region}.amazonaws.com/docs/{name}")
        print(f"anonymous storage read: HTTP {response.status_code}", flush=True)
        if response.status_code not in (401, 403, 404):
            raise RuntimeError("Anonymous storage reads were not denied")
    response = http.get(f"https://{args.bucket}.s3.{args.region}.amazonaws.com/")
    print(f"anonymous bucket listing: HTTP {response.status_code}", flush=True)
    if response.status_code not in (401, 403):
        raise RuntimeError("Anonymous bucket listing was not denied")


def measure_acl_change(http, args, allowed, denied, reingest, timeout=1500, interval=10, clean_streak=6, sleep=time.sleep):
    """Swap the restricted file's reader from A to B, re-sync, and time when the platform enforces it.

    Passes only when B gets the fact and A is denied for ``clean_streak`` consecutive checks.
    """
    url = args.api_url.rstrip("/") + "/ask"
    approved_name = args.approved_file.split("/")[-1]

    def sees(token):
        data = http.post(url, headers={"Authorization": f"Bearer {resolve(token)}"},
                         json={"input": args.question, "store": False, "background": False}).json()
        return args.expected.casefold() in str(data.get("text", "")).casefold() or approved_name in data.get("sources", [])

    reingest()
    started, streak, first_b, last_a = time.monotonic(), 0, None, None
    while streak < clean_streak:
        elapsed = int(time.monotonic() - started)
        if elapsed > timeout:
            raise RuntimeError(f"ACL change did not settle within {timeout}s (B saw it: {first_b}s, A last saw it: {last_a}s)")
        a_sees, b_sees = sees(allowed), sees(denied)
        if b_sees and first_b is None:
            first_b = elapsed
        if a_sees:
            last_a = elapsed
        streak = streak + 1 if (b_sees and not a_sees) else 0
        print(f"acl change +{elapsed}s: A sees={a_sees}, B sees={b_sees}, streak={streak}", flush=True)
        if streak < clean_streak:
            sleep(interval)
    print(f"ACL change enforced: B first granted at {first_b}s, A last exposed at {last_a}s, stable after "
          f"{int(time.monotonic() - started)}s", flush=True)
    return {"granted_seconds": first_b, "last_revoked_exposure_seconds": last_a}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for field in ("api-url", "bucket", "region", "knowledge-base-id", "data-source-id"):
        parser.add_argument(f"--{field}", required=True)
    for field in CASE_FIELDS:
        parser.add_argument(f"--{field.replace('_', '-')}")
    parser.add_argument("--config", type=Path, required=True, help="Acceptance JSON (see guides/protected-mode.md)")
    parser.add_argument("--user-pool-id", help="Needed with --mint-users")
    parser.add_argument("--client-id", help="Needed with --mint-users")
    parser.add_argument("--function-name", help="Also check that a direct invoke without claims is rejected")
    parser.add_argument("--allow-root-operator", action="store_true",
                        help="Do not fail when run as the account root user (the lock check cannot be verified as root)")
    parser.add_argument("--mint-users", action="store_true",
                        help="Create two synthetic Cognito users, mint tokens locally (never printed), delete them afterwards")
    parser.add_argument("--acl-change-test", action="store_true",
                        help="Afterwards swap the reader from A to B, re-sync and time the enforcement")
    parser.add_argument("--ready-timeout", type=int, default=600)
    parser.add_argument("--settle-timeout", type=int, default=1500)
    parser.add_argument("--clean-streak", type=int, default=30, help="Consecutive clean denied checks required")
    args = parser.parse_args()
    if not args.api_url.startswith("https://") or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", args.bucket):
        parser.error("Use the https API URL and the private bucket name")
    try:
        vars(args).update(load_config(args.config))
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from ingest_protected import corpus_files

        root, files = corpus_files(args.directory)
        if {p.relative_to(root).as_posix() for p in files} != {args.approved_file, args.control_file}:
            raise ValueError("Use a dedicated directory containing exactly the two named synthetic fixtures")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.mint_users and not (args.user_pool_id and args.client_id):
        parser.error("--mint-users needs --user-pool-id and --client-id")
    if not args.mint_users and not sys.stdin.isatty():
        parser.error("Run in a terminal; tokens are prompted without echo and are never saved (or use --mint-users)")
    if args.acl_change_test and not args.mint_users:
        parser.error("--acl-change-test needs --mint-users (tokens must be re-minted while it runs)")

    import boto3
    from ingest_protected import ingest

    # The control is readable by a third identity that neither test user is, so it IS indexed and ACL-filtered.
    control_owner = "control-owner@" + args.allowed_user.split("@")[1]

    def run_ingest(users_a):
        manifest = {args.approved_file: {"users": users_a}, args.control_file: {"users": [control_owner]}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(manifest, handle)
        try:
            ingest(args.bucket, args.knowledge_base_id, args.data_source_id, args.directory, handle.name, region=args.region)
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"Live protected ingestion failed: {type(exc).__name__}: {exc}") from None
        finally:
            Path(handle.name).unlink(missing_ok=True)

    users = None
    try:
        if args.mint_users:
            cognito = boto3.client("cognito-idp", region_name=args.region)
            users = CognitoUsers(cognito, args.user_pool_id, args.client_id)
            users.create(args.allowed_user)
            users.create(args.denied_user)
            allowed, denied, expired = (lambda: users.token(args.allowed_user)), (lambda: users.token(args.denied_user)), None
        else:
            labels = ["Approved user's Cognito ID token", "Denied user's Cognito ID token",
                      "Approved user's EXPIRED ID token (Enter to skip)"]
            allowed, denied, expired = (read_token(label) for label in labels)
            if not allowed or not denied or any(c.isspace() for c in allowed + denied) or allowed == denied:
                parser.error("Supply two distinct ID tokens without a Bearer prefix")
        run_ingest([args.allowed_user])

        def direct_retrieve():
            if boto3.client("sts").get_caller_identity()["Arn"].endswith(":root"):
                return "root-exempt"
            try:
                boto3.client("bedrock-agent-runtime", region_name=args.region).retrieve(
                    knowledgeBaseId=args.knowledge_base_id, retrievalQuery={"text": args.question},
                    userContext={"userId": args.allowed_user})
            except Exception as exc:  # noqa: BLE001
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                return "denied" if code == "AccessDeniedException" else f"error {code or type(exc).__name__}"
            return "allowed"

        def direct_invoke():
            lam = boto3.client("lambda", region_name=args.region)
            payload = {"requestContext": {}, "body": json.dumps({"input": args.question, "store": False})}
            out = lam.invoke(FunctionName=args.function_name, Payload=json.dumps(payload).encode())
            return json.loads(out["Payload"].read()).get("statusCode")

        with httpx.Client(timeout=90, trust_env=False, follow_redirects=False) as http:
            run_tests(http, args, allowed, denied, expired or None, args.ready_timeout, args.settle_timeout, args.clean_streak,
                      direct_retrieve=direct_retrieve, direct_invoke=direct_invoke if args.function_name else None,
                      allow_root_operator=args.allow_root_operator)
            if args.acl_change_test:
                measure_acl_change(http, args, allowed, denied, lambda: run_ingest([args.denied_user]))
    except (httpx.HTTPError, RuntimeError, TypeError, KeyError) as exc:
        detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        raise SystemExit(f"Live protected acceptance failed: {detail}") from None
    finally:
        if users is not None:
            users.delete_all()
    print("Live protected acceptance passed.")


if __name__ == "__main__":
    main()
