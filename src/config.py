"""Application configuration loaded entirely from environment variables.

Pattern for multiple targets uses indexed variables:

targets:
  - app_name: 
    name: 
    repository: 
    tag: 
    image_pattern: 
    
  - app_name: 
    name: 
    repository: 
    tag: 
    image_pattern: 

Auth strategies:
  ECR: IRSA (default on K8s) or explicit AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY
  ArgoCD: ARGOCD_AUTH_TOKEN or ARGOCD_USERNAME + ARGOCD_PASSWORD
"""

import os
import logging
import sys
import yaml

from dataclasses import dataclass
from typing import List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeploymentTarget:
    """A single application deployment to monitor."""

    name: str
    repository: str
    tag: str
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

    targets: List[DeploymentTarget]

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


def _load_targets_config():
    config_path = os.getenv(
        "TARGETS_CONFIG_PATH",
        Path(__file__).parent / "targets.yaml",
    )

    with open(config_path) as f:
        return yaml.safe_load(f)

    
def load_config() -> AppConfig:
    """Load full configuration from environment variables."""

    aws_access_key_id = os.environ.get("AWS_ACCESS_KEY_ID")
    aws_secret_access_key = os.environ.get("AWS_SECRET_ACCESS_KEY")

    argocd_auth_token = os.environ.get("ARGOCD_AUTH_TOKEN")
    argocd_username = os.environ.get("ARGOCD_USERNAME")
    argocd_password = os.environ.get("ARGOCD_PASSWORD")

    targets_config = _load_targets_config()
    targets = [
        DeploymentTarget(**target)
        for target in targets_config["targets"]
    ]

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
        targets=targets,
        metrics_port=int(os.environ.get("METRICS_PORT", "8000")),
        poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "60")),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
