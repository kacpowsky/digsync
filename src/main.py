"""Digsync - ECR and ArgoCD image digest synchronization monitor."""

import logging
import signal
import sys
import time

from prometheus_client import start_http_server

from src.config import load_config
from src.ecr_client import ECRClient
from src.argocd_client import ArgoCDClient
from src.metrics import update_metrics

logger = logging.getLogger(__name__)

shutdown_requested = False


def _handle_signal(signum: int, _frame) -> None:
    global shutdown_requested
    logger.info("Received signal %d, shutting down gracefully...", signum)
    shutdown_requested = True


def main() -> None:
    """Application entrypoint."""
    config = load_config()

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
    )

    logger.info("Starting digsync with poll interval %ds", config.poll_interval_seconds)
    logger.info(
        "Monitoring %d ECR targets and %d ArgoCD targets",
        len(config.ecr_targets),
        len(config.argocd_targets),
    )

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    start_http_server(config.metrics_port)
    logger.info("Prometheus metrics server started on port %d", config.metrics_port)

    ecr_client = ECRClient(config)
    argocd_client = ArgoCDClient(config)

    while not shutdown_requested:
        try:
            update_metrics(config, ecr_client, argocd_client)
        except Exception:
            logger.exception("Unexpected error during metrics update")

        elapsed = 0
        while elapsed < config.poll_interval_seconds and not shutdown_requested:
            time.sleep(1)
            elapsed += 1

    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
