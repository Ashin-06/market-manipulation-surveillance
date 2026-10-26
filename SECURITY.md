# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately, through GitHub's private vulnerability
reporting on this repository (Security → Report a vulnerability) or by contacting the
maintainer, [@Ashin-06](https://github.com/Ashin-06), directly. Do not open a public issue.

Include what you found, how to reproduce it and what an attacker could do with it. You should
get an acknowledgement within 5 working days. Please give us 90 days to fix an issue before
disclosing it, and don't test against anyone else's deployment.

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | yes |
| < 1.0 | no (prototype releases) |

## Scope

In scope: the surveillance API, the order book and detectors, the alert store, the container
image and the CI configuration in this repository.

Out of scope: ways to manipulate a market without being flagged. The detection thresholds are
in the source and the README; evading hand-set rules is a known limit (see
`docs/model-card.md`), not a vulnerability. Report it if you think the rules are badly wrong,
but as a normal issue.

This project has **not** had an independent security review or penetration test.
