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
from src.argocd_client import ArgoCDClient

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
digest_in_sync = Gauge(
    "digsync_digest_in_sync",
    "1 if the ECR built digest matches the ArgoCD deployed digest, 0 if mismatch, -1 if unknown",
    ["name", "repository", "app_name"],
)

# Operational health
ecr_scrape_success = Gauge(
    "digsync_ecr_scrape_success",
    "1 if the last ECR scrape succeeded, 0 if it failed",
    ["name", "repository", "tag"],
)

argocd_scrape_success = Gauge(
    "digsync_argocd_scrape_success",
    "1 if the last ArgoCD scrape succeeded, 0 if it failed",
    ["name", "app_name"],
)


class _DigestStore:
    """Holds last known digests for comparison across sources."""

    def __init__(self):
        self.ecr: dict[str, str] = {}
        self.argocd: dict[str, str] = {}


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
        digest = argocd_client.get_running_digest(target)
        if digest:
            argocd_deployed_digest.labels(
                name=target.name,
                app_name=target.app_name,
                image_pattern=target.image_pattern,
                digest=digest,
            ).set(1)
            argocd_scrape_success.labels(
                name=target.name,
                app_name=target.app_name,
            ).set(1)
            _store.argocd[target.name] = digest
        else:
            argocd_scrape_success.labels(
                name=target.name,
                app_name=target.app_name,
            ).set(0)

    _update_sync_status(config)

    logger.info("Metrics update cycle complete")


def _update_sync_status(config: AppConfig) -> None:
    """Compare ECR and ArgoCD digests by matching target names."""

    ecr_by_name = {t.name: t for t in config.ecr_targets}
    argocd_by_name = {t.name: t for t in config.argocd_targets}

    all_names = set(ecr_by_name.keys()) & set(argocd_by_name.keys())

    for name in all_names:
        ecr_target = ecr_by_name[name]
        argocd_target = argocd_by_name[name]

        ecr_dig = _store.ecr.get(name)
        argo_dig = _store.argocd.get(name)

        if ecr_dig and argo_dig:
            value = 1.0 if ecr_dig == argo_dig else 0.0
        else:
            value = -1.0

        digest_in_sync.labels(
            name=name,
            repository=ecr_target.repository,
            app_name=argocd_target.app_name,
        ).set(value)
