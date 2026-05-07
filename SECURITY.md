# Security Policy

## Reporting a vulnerability

If you've found a security vulnerability — particularly anything that could
lead to fund loss, signature forgery, or unauthorized state changes on a
contract or validator — please report it privately rather than opening a
public issue.

**Email**: stalkervrm@proton.me

Please include:

- A description of the vulnerability and its impact.
- Steps to reproduce, or a proof-of-concept if you have one.
- Your assessment of severity (low / medium / high / critical).
- Whether you've disclosed this to anyone else.

We will acknowledge receipt within 72 hours and aim to provide a substantive
response (assessment + remediation timeline) within 7 days for high-severity
issues.

## Scope

In scope across the [Subnet 112 (Minotaur)](https://github.com/subnet112)
repos:

- Smart-contract bugs in `subnet112/minotaur_contracts` and
  `subnet112/minotaur-apps` (`contracts/`)
- Validator / API logic in `subnet112/minotaur_subnet` that could break
  consensus, leak signing keys, or accept invalid plans
- Solver-sandbox escape in `subnet112/minotaur-solver`'s screening pipeline
- Anything that can convert a non-privileged caller into a privileged one

Out of scope:

- Issues requiring physical access to a validator's machine.
- Denial-of-service requiring the attacker to spend more than 10x what they
  cost the network.
- Vulnerabilities in third-party dependencies that have already been
  reported upstream — please report those to the upstream project.

## Coordinated disclosure

Once a fix is in production we will publicly credit the reporter (unless
they prefer to remain anonymous) and publish a short writeup.
