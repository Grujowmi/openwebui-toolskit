# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Use GitHub's private vulnerability reporting: open the **Security** tab of this repository and choose **Report a vulnerability**. Include the Hélios version, the Open WebUI version, what you did, what you expected and what happened.

This is a one-person project. I will acknowledge a report as soon as I can and keep you informed of the fix. I cannot promise a fixed delay.

## Supported versions

Only the latest release is supported. Hélios is written and tested against Open WebUI 0.13.0.

## Scope

In scope:

- Access control of the Hélios page and API routes (administrator only).
- Cross-site request issues on the POST routes.
- Injection into the page, the CSV export or the email report.
- Any path by which message content, or data of users other than what is documented, could be read, stored or sent.
- The recipient check of the email report (only Open WebUI administrator accounts).

Out of scope:

- Vulnerabilities in Open WebUI itself. Report them to the [Open WebUI project](https://github.com/open-webui/open-webui).
- Anything that requires an administrator to act maliciously. An Open WebUI administrator can already run arbitrary code through Functions.
- Misconfiguration, for example an open SMTP relay or a leaked SMTP password.

## Things to know before you install

- Hélios is an Open WebUI Function. It runs inside the Open WebUI process with the same privileges. Read the single file before you paste it.
- It creates and writes a `helios` schema in your Open WebUI database and reads `chat_message` and `model`. It never reads message content.
- Prefer the `HELIOS_SMTP_PASSWORD` environment variable over the `smtp_password` valve, which is stored in the Open WebUI database.
- Per-user figures are personal data in many jurisdictions. Use `pseudonymize_users` and `retention_months` as fits your obligations.
