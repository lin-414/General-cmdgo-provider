# Security policy

## Do not report credentials

Never include any of the following in an issue, pull request, log upload, or screenshot:

- `%APPDATA%\\cmdgo-provider\\token.json`
- OAuth tokens
- Authorization headers
- Full proxy logs containing request details

If a token was exposed, revoke or re-authenticate the Command Code session immediately.

## Local-only operation

The adapter is intended for one user on one computer. Keep it bound to `127.0.0.1` and do not place it behind a public reverse proxy.

## Vulnerability reports

Please report code vulnerabilities privately to the repository owner rather than publishing working token-exfiltration steps in a public issue.
