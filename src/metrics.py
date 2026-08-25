"""Prometheus metrics definitions and update logic.

Metrics are designed for dashboard comparison:
- ecr_digest: what was BUILT (latest in registry)
- argocd_digest: what is DEPLOYED (running in cluster)
- digest_match: whether they are in sync
"""

import logging

from prometheus_client import Gauge

from src.config import AppConfig
from src.ecr_client import ECRClient
from src.argocd_client import ArgoCDClient, DigestStatus

logger = logging.getLogger(__name__)

# Raw digest exposed as a label value - queryable in Grafana.
# The 'pushed_at' label carries the ISO 8601 date the image was pushed to ECR,
# so a dashboard can show how old the built image is.
ecr_built_digest = Gauge(
    "digsync_ecr_built_digest_hash",
    "Constant 1 gauge; the 'digest' label holds the latest ECR image digest (built)",
    ["name", "repository", "tag", "digest", "pushed_at"],
)

argocd_deployed_digest = Gauge(
    "digsync_argocd_deployed_digest_hash",
    "Constant 1 gauge; the 'digest' label holds the running ArgoCD digest (deployed)",
    ["name", "app_name", "image_pattern", "digest"],
)

# Comparison metric
#   1  = ECR built digest matches ArgoCD deployed digest (in sync)
#   0  = both digests present but DIFFERENT (real drift - the only "bad" case)
#   2  = service not deployed in ArgoCD (disabled in dev) - intentionally NOT an error
#  -1  = unknown (e.g. ECR scrape failed, so no comparison is possible)
digest_in_sync = Gauge(
    "digsync_digest_in_sync",
    "1 in sync, 0 mismatch (drift), 2 not deployed in ArgoCD (disabled), -1 unknown",
    ["name", "repository", "app_name"],
)

# Operational health
ecr_scrape_success = Gauge(
    "digsync_ecr_scrape_success",
    "1 if the last ECR scrape succeeded, 0 if it failed",
    ["name", "repository", "tag"],
)

# Note: a service being disabled/not deployed in ArgoCD is NOT a scrape failure.
# scrape_success drops to 0 only on real errors (auth, timeout, CLI issues).
argocd_scrape_success = Gauge(
    "digsync_argocd_scrape_success",
    "1 if the last ArgoCD scrape succeeded (incl. 'not deployed'), 0 only on real errors",
    ["name", "app_name"],
)

# Whether the service is currently deployed in ArgoCD at all.
#   1 = deployed (digest found)
#   0 = not deployed / disabled (expected in dev, not an error)
argocd_deployed = Gauge(
    "digsync_argocd_deployed",
    "1 if the service is deployed in ArgoCD, 0 if disabled / not deployed",
    ["name", "app_name"],
)


class _DigestStore:
    """Holds last known digests and deployment state for comparison across sources."""

    def __init__(self):
        # key: (repository, tag) -> value: (digest, pushed_at)
        self.ecr: dict[tuple[str, str], tuple[str, str]] = {}

        # key: (app_name, image_pattern)
        self.argocd: dict[tuple[str, str], str] = {}

        # key: (app_name, image_pattern)
        self.argocd_not_deployed: set[tuple[str, str]] = set()


_store = _DigestStore()


def update_metrics(config: AppConfig, ecr_client: ECRClient, argocd_client: ArgoCDClient) -> None:
    """Fetch digests from ECR and ArgoCD and update Prometheus metrics."""

    # ECR is queried once per unique service.
    #
    # A service can have multiple ArgoCD targets, but they all use the
    # same ECR repository/tag.
    ecr_targets = {}

    for target in config.targets:
        ecr_key = (target.repository, target.tag)
        ecr_targets.setdefault(ecr_key, target)

    for target in ecr_targets.values():
        ecr_key = (target.repository, target.tag)
        image = ecr_client.get_image_digest(target)

        if image:
            digest = image.digest
            pushed_at = image.pushed_at

            old = _store.ecr.get(ecr_key)

            if old and old != (digest, pushed_at):
                old_digest, old_pushed_at = old
                ecr_built_digest.remove(target.name, target.repository, target.tag, old_digest, old_pushed_at)

            ecr_built_digest.labels(
                name=target.name,
                repository=target.repository,
                tag=target.tag,
                digest=digest,
                pushed_at=pushed_at,
            ).set(1)
            ecr_scrape_success.labels(name=target.name, repository=target.repository, tag=target.tag).set(1)

            _store.ecr[ecr_key] = (digest, pushed_at)

        else:
            _store.ecr.pop(ecr_key, None)

            ecr_scrape_success.labels(name=target.name, repository=target.repository, tag=target.tag).set(0)

    # Every ArgoCD application is queried separately.
    #
    # This is important because one service can have multiple ArgoCD
    # applications, e.g. sbt-coach-user-profiles.
    for target in config.targets:
        result = argocd_client.get_running_digest(target)

        app_key = (target.app_name, target.image_pattern)

        if result.status == DigestStatus.FOUND:
            old_digest = _store.argocd.get(app_key)

            if old_digest and old_digest != result.digest:
                argocd_deployed_digest.remove(target.name, target.app_name, target.image_pattern, old_digest)

            argocd_deployed_digest.labels(name=target.name, app_name=target.app_name, image_pattern=target.image_pattern, digest=result.digest).set(1)
            argocd_scrape_success.labels(name=target.name, app_name=target.app_name).set(1)

            argocd_deployed.labels(name=target.name, app_name=target.app_name).set(1)

            _store.argocd[app_key] = result.digest
            _store.argocd_not_deployed.discard(app_key)

        elif result.status == DigestStatus.NOT_DEPLOYED:
            # Service is disabled / not deployed in ArgoCD.
            # This is expected in dev and is NOT a scrape failure.

            old_digest = _store.argocd.pop(app_key, None)

            if old_digest: 
                argocd_deployed_digest.remove(target.name, target.app_name, target.image_pattern, old_digest)

            argocd_scrape_success.labels(name=target.name, app_name=target.app_name).set(1)
            argocd_deployed.labels(name=target.name, app_name=target.app_name).set(0)

            _store.argocd_not_deployed.add(app_key)

        else:
            # DigestStatus.ERROR
            _store.argocd.pop(app_key, None)
            _store.argocd_not_deployed.discard(app_key)

            argocd_scrape_success.labels(name=target.name, app_name=target.app_name).set(0)

    _update_sync_status(config)

    logger.info("Metrics update cycle complete")


def _update_sync_status(config: AppConfig) -> None:
    """Compare ECR and ArgoCD digests for every deployment target.

    Drift (value 0) is only reported when BOTH digests are present and differ.

    A service that is disabled / not deployed in ArgoCD is reported as 2
    (not an error), so toggling services in dev never produces false
    mismatch alerts.
    """

    for target in config.targets:
        ecr_entry = _store.ecr.get((target.repository, target.tag))
        ecr_digest = ecr_entry[0] if ecr_entry else None
        app_key = (target.app_name, target.image_pattern)

        argocd_digest = _store.argocd.get(app_key)

        if app_key in _store.argocd_not_deployed:
            # Disabled in ArgoCD - explicitly NOT a mismatch.
            value = 2.0

        elif ecr_digest and argocd_digest:
            value = 1.0 if ecr_digest == argocd_digest else 0.0

        else:
            # ECR or ArgoCD data is missing due to a real scrape problem.
            value = -1.0

        digest_in_sync.labels(
            name=target.name,
            repository=target.repository,
            app_name=target.app_name,
        ).set(value)
