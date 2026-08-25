"""AWS ECR client for fetching image digests.

Supports two auth strategies:
- IRSA (IAM Role for Service Account): leave AWS_ACCESS_KEY_ID empty, boto3 uses pod identity
- Explicit keys: set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
"""

import logging
from typing import NamedTuple, Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from src.config import AppConfig, DeploymentTarget

logger = logging.getLogger(__name__)


class ImageInfo(NamedTuple):
    """Digest and push date of the latest ECR image for a tag."""

    digest: str
    # ISO 8601 timestamp of when the image was pushed to ECR (imagePushedAt),
    # or "" if ECR did not return it. Exposed as a Prometheus label so a
    # Grafana dashboard can show how old the built image is.
    pushed_at: str


class ECRClient:
    """Client for interacting with AWS ECR to fetch image digests."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._client = self._build_client(config)

    @staticmethod
    def _build_client(config: AppConfig):
        """Build boto3 ECR client with IRSA or explicit credentials."""
        kwargs = {"region_name": config.aws_region}

        if config.aws_access_key_id and config.aws_secret_access_key:
            kwargs["aws_access_key_id"] = config.aws_access_key_id
            kwargs["aws_secret_access_key"] = config.aws_secret_access_key
            logger.info("ECR auth: using explicit access key")
        else:
            logger.info("ECR auth: using IRSA / default credential chain")

        return boto3.client("ecr", **kwargs)

    def get_image_digest(self, target: DeploymentTarget) -> Optional[ImageInfo]:
        """Fetch the digest and push date for a specific image tag from ECR.

        Returns an ImageInfo (digest + pushed_at) or None on failure.
        """
        try:
            response = self._client.describe_images(
                registryId=self._config.ecr_registry_id,
                repositoryName=target.repository,
                imageIds=[{"imageTag": target.tag}],
            )

            image_details = response.get("imageDetails", [])
            if not image_details:
                logger.warning(
                    "No image found for %s:%s",
                    target.repository,
                    target.tag,
                )
                return None

            latest = sorted(
                image_details,
                key=lambda x: x.get("imagePushedAt", ""),
                reverse=True,
            )[0]
            digest = latest.get("imageDigest")

            if not digest:
                logger.warning(
                    "Image found for %s:%s but no digest was returned",
                    target.repository,
                    target.tag,
                )
                return None

            pushed_at_raw = latest.get("imagePushedAt")
            pushed_at = pushed_at_raw.isoformat() if pushed_at_raw else ""

            logger.info(
                "ECR digest for %s:%s -> %s (pushed_at=%s)",
                target.repository,
                target.tag,
                digest,
                pushed_at or "unknown",
            )
            return ImageInfo(digest=digest, pushed_at=pushed_at)

        except (ClientError, NoCredentialsError) as exc:
            logger.error(
                "Failed to fetch ECR digest for %s:%s: %s",
                target.repository,
                target.tag,
                exc,
            )
            return None
