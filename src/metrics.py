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

# Raw digest exposed as a label value - queryable in Grafana
ecr_built_digest = Gauge(
    "digsync_ecr_built_digest_hash",
    "Constant 1 gauge; the 'digest' label holds the latest ECR image digest (built)",
    ["name", "repository", "tag", "digest"],
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
        self.ecr: dict[str, str] = {}
        self.argocd: dict[str, str] = {}
        # Names of services currently disabled / not deployed in ArgoCD.
        self.argocd_not_deployed: set[str] = set()


_store = _DigestStore()


def update_metrics(config: AppConfig, ecr_client: ECRClient, argocd_client: ArgoCDClient) -> None:
    """Fetch digests from ECR and ArgoCD, update all Prometheus metrics."""

    for target in config.ecr_targets:
        digest = ecr_client.get_image_digest(target)
        if digest:
            ecr_built_digest.labels(
                name=target.name,
                repository=target.repository,
                tag=target.tag,
                digest=digest,
            ).set(1)
            ecr_scrape_success.labels(
                name=target.name,
                repository=target.repository,
                tag=target.tag,
            ).set(1)
            _store.ecr[target.name] = digest
        else:
            ecr_scrape_success.labels(
                name=target.name,
                repository=target.repository,
                tag=target.tag,
            ).set(0)

    for target in config.argocd_targets:
        result = argocd_client.get_running_digest(target)

        if result.status == DigestStatus.FOUND:
            argocd_deployed_digest.labels(
                name=target.name,
                app_name=target.app_name,
                image_pattern=target.image_pattern,
                digest=result.digest,
            ).set(1)
            # A successful scrape that found a deployed workload.
            argocd_scrape_success.labels(name=target.name, app_name=target.app_name).set(1)
            argocd_deployed.labels(name=target.name, app_name=target.app_name).set(1)
            _store.argocd[target.name] = result.digest
            _store.argocd_not_deployed.discard(target.name)

        elif result.status == DigestStatus.NOT_DEPLOYED:
            # Service is disabled / not deployed in ArgoCD. This is expected in dev
            # and is NOT a failure: the scrape itself succeeded, there's just nothing
            # to compare against.
            argocd_scrape_success.labels(name=target.name, app_name=target.app_name).set(1)
            argocd_deployed.labels(name=target.name, app_name=target.app_name).set(0)
            _store.argocd.pop(target.name, None)
            _store.argocd_not_deployed.add(target.name)

        else:  # DigestStatus.ERROR
            argocd_scrape_success.labels(name=target.name, app_name=target.app_name).set(0)
            _store.argocd_not_deployed.discard(target.name)

    _update_sync_status(config)

    logger.info("Metrics update cycle complete")


def _update_sync_status(config: AppConfig) -> None:
    """Compare ECR and ArgoCD digests by matching target names.

    Drift (value 0) is only reported when BOTH digests are present and differ.
    A service that is disabled / not deployed in ArgoCD is reported as 2 (not an
    error) so that toggling services in dev never produces false mismatch alerts.
    """

    ecr_by_name = {t.name: t for t in config.ecr_targets}
    argocd_by_name = {t.name: t for t in config.argocd_targets}

    all_names = set(ecr_by_name.keys()) & set(argocd_by_name.keys())

    for name in all_names:
        ecr_target = ecr_by_name[name]
        argocd_target = argocd_by_name[name]

        ecr_dig = _store.ecr.get(name)
        argo_dig = _store.argocd.get(name)

        if name in _store.argocd_not_deployed:
            # Disabled in ArgoCD - explicitly NOT a mismatch.
            value = 2.0
        elif ecr_dig and argo_dig:
            value = 1.0 if ecr_dig == argo_dig else 0.0
        else:
            # ECR (or ArgoCD) data missing due to a real scrape problem.
            value = -1.0

        digest_in_sync.labels(
            name=name,
            repository=ecr_target.repository,
            app_name=argocd_target.app_name,
        ).set(value)
