"""Application configuration loaded entirely from environment variables.

Pattern for multiple targets uses indexed variables:
  ECR_TARGET_1_NAME, ECR_TARGET_1_REPOSITORY, ECR_TARGET_1_TAG
  ECR_TARGET_2_NAME, ECR_TARGET_2_REPOSITORY, ECR_TARGET_2_TAG
  ...
  ARGOCD_TARGET_1_NAME, ARGOCD_TARGET_1_APP_NAME, ARGOCD_TARGET_1_IMAGE_PATTERN
  ...

Auth strategies:
  ECR: IRSA (default on K8s) or explicit AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY
  ArgoCD: ARGOCD_AUTH_TOKEN or ARGOCD_USERNAME + ARGOCD_PASSWORD
"""

import os
import logging
import sys
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ECRImageTarget:
    """A single ECR image to monitor."""

    name: str
    repository: str
    tag: str


@dataclass(frozen=True)
class ArgoDeploymentTarget:
    """A single ArgoCD deployment to monitor."""

    name: str
    app_name: str
    image_pattern: str


@dataclass(frozen=True)
class AppConfig:
    """Full application configuration."""

    # AWS - optional when using IRSA
    aws_region: str
    aws_access_key_id: Optional[str]
    aws_secret_access_key: Optional[str]
    ecr_registry_id: str

    # ArgoCD - token OR username+password
    argocd_server: str
    argocd_auth_token: Optional[str]
    argocd_username: Optional[str]
    argocd_password: Optional[str]
    argocd_grpc_web: bool

    ecr_targets: List[ECRImageTarget]
    argocd_targets: List[ArgoDeploymentTarget]

    metrics_port: int
    poll_interval_seconds: int
    log_level: str


def _require_env(key: str) -> str:
    """Get a required environment variable or exit."""
    value = os.environ.get(key)
    if not value:
        logger.error("Required environment variable %s is not set", key)
        sys.exit(1)
    return value


def _parse_ecr_targets() -> List[ECRImageTarget]:
    """Parse indexed ECR target variables.

    Reads ECR_TARGETS_COUNT then iterates:
      ECR_TARGET_{i}_NAME
      ECR_TARGET_{i}_REPOSITORY
      ECR_TARGET_{i}_TAG
    """
    count = int(os.environ.get("ECR_TARGETS_COUNT", "0"))
    if count == 0:
        logger.error("ECR_TARGETS_COUNT is not set or is 0")
        sys.exit(1)

    targets = []
    for i in range(1, count + 1):
        prefix = f"ECR_TARGET_{i}"
        name = _require_env(f"{prefix}_NAME")
        repository = _require_env(f"{prefix}_REPOSITORY")
        tag = os.environ.get(f"{prefix}_TAG", "latest")

        targets.append(ECRImageTarget(name=name, repository=repository, tag=tag))

    return targets


def _parse_argocd_targets() -> List[ArgoDeploymentTarget]:
    """Parse indexed ArgoCD target variables.

    Reads ARGOCD_TARGETS_COUNT then iterates:
      ARGOCD_TARGET_{i}_NAME
      ARGOCD_TARGET_{i}_APP_NAME
      ARGOCD_TARGET_{i}_IMAGE_PATTERN
    """
    count = int(os.environ.get("ARGOCD_TARGETS_COUNT", "0"))
    if count == 0:
        logger.error("ARGOCD_TARGETS_COUNT is not set or is 0")
        sys.exit(1)

    targets = []
    for i in range(1, count + 1):
        prefix = f"ARGOCD_TARGET_{i}"
        name = _require_env(f"{prefix}_NAME")
        app_name = _require_env(f"{prefix}_APP_NAME")
        image_pattern = _require_env(f"{prefix}_IMAGE_PATTERN")

        targets.append(ArgoDeploymentTarget(name=name, app_name=app_name, image_pattern=image_pattern))

    return targets


def load_config() -> AppConfig:
    """Load full configuration from environment variables."""

    aws_access_key_id = os.environ.get("AWS_ACCESS_KEY_ID")
    aws_secret_access_key = os.environ.get("AWS_SECRET_ACCESS_KEY")

    argocd_auth_token = os.environ.get("ARGOCD_AUTH_TOKEN")
    argocd_username = os.environ.get("ARGOCD_USERNAME")
    argocd_password = os.environ.get("ARGOCD_PASSWORD")

    if not argocd_auth_token and not (argocd_username and argocd_password):
        logger.error("Either ARGOCD_AUTH_TOKEN or both ARGOCD_USERNAME and ARGOCD_PASSWORD must be set")
        sys.exit(1)

    return AppConfig(
        aws_region=_require_env("AWS_REGION"),
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        ecr_registry_id=_require_env("ECR_REGISTRY_ID"),
        argocd_server=_require_env("ARGOCD_SERVER"),
        argocd_auth_token=argocd_auth_token,
        argocd_username=argocd_username,
        argocd_password=argocd_password,
        argocd_grpc_web=os.environ.get("ARGOCD_GRPC_WEB", "true").lower() == "true",
        ecr_targets=_parse_ecr_targets(),
        argocd_targets=_parse_argocd_targets(),
        metrics_port=int(os.environ.get("METRICS_PORT", "8000")),
        poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "60")),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
