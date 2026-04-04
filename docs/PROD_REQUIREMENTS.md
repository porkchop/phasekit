# Production requirements

Tailor this file to the product, but keep the categories explicit.

## Security
- principle of least privilege
- secrets are not committed to the repo
- authentication and authorization are explicit
- user-owned actions are server-authoritative
- abuse controls exist for public-facing endpoints

## Reliability
- deployments are repeatable
- backups and restore paths are documented
- migrations are reversible or carefully staged
- failure modes are documented for critical dependencies

## Observability
- structured logging
- error reporting
- health checks
- metrics or dashboards for key flows

## Performance
- latency or throughput expectations defined
- obvious hotspots measured before optimization

## Operations
- environment variables documented
- runbooks exist for deploy / rollback / incident handling
- at least one production readiness review occurs before launch

## Product readiness
- onboarding is understandable
- browser or client QA passes on the main workflow
- known limitations are documented
