"""AWS ECR client for fetching image digests.

Supports two auth strategies:
- IRSA (IAM Role for Service Account): leave AWS_ACCESS_KEY_ID empty, boto3 uses pod identity
- Explicit keys: set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
"""

import logging
from typing import Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from src.config import AppConfig, ECRImageTarget

logger = logging.getLogger(__name__)


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

    def get_image_digest(self, target: ECRImageTarget) -> Optional[str]:
        """Fetch the digest for a specific image tag from ECR.

        Returns the image digest (sha256:...) or None on failure.
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

            logger.info(
                "ECR digest for %s:%s -> %s",
                target.repository,
                target.tag,
                digest,
            )
            return digest

        except (ClientError, NoCredentialsError) as exc:
            logger.error(
                "Failed to fetch ECR digest for %s:%s: %s",
                target.repository,
                target.tag,
                exc,
            )
            return None
