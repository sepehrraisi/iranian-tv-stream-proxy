# Security policy

## Reporting

Please use GitHub private vulnerability reporting for security issues. Do not publish provider credentials, signed playlist URLs, access tokens, server addresses, or exploit details in an issue.

## Deployment

- Never commit provider API credentials or signed HLS URLs.
- Bind the service to loopback and place it behind an HTTPS reverse proxy.
- Protect `/stats`, `/warm`, `/resolve`, and operational endpoints from public access.
- Keep the upstream host allowlist enabled.
- Use a long random `STATS_TOKEN`.
- Respect broadcaster terms, territorial restrictions, and content rights.
- Do not use this software to bypass authentication, DRM, paywalls, or access controls.
