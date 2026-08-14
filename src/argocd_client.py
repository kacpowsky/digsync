"""ArgoCD client for fetching running deployment digests.

Supports two auth strategies:
- Token: set ARGOCD_AUTH_TOKEN (e.g. from argocd account generate-token)
- Username/Password: set ARGOCD_USERNAME and ARGOCD_PASSWORD
"""

import logging
import re
import subprocess
import yaml

from enum import Enum
from typing import NamedTuple, Optional

from src.config import AppConfig, DeploymentTarget

logger = logging.getLogger(__name__)

DIGEST_PATTERN = re.compile(r"@(sha256:[a-f0-9]{64})")

_TOKEN_EXPIRED_INDICATORS = (
    "token is expired",
    "token has invalid claims",
    "invalid session",
    "Unauthenticated",
)

# Indicators that the application simply does not exist in ArgoCD.
# This is NOT an error: in dev, services are frequently disabled / not deployed,
# and we don't want missing apps to flip scrape_success to a failure state.
_APP_NOT_FOUND_INDICATORS = (
    "code = NotFound",
    "not found",
    "does not exist",
)


class DigestStatus(Enum):
    """Outcome of fetching a running digest from ArgoCD."""

    FOUND = "found"  # digest successfully extracted
    NOT_DEPLOYED = "not_deployed"  # app/service is disabled or absent (NOT an error)
    ERROR = "error"  # real failure (auth, timeout, CLI missing, etc.)


class DigestResult(NamedTuple):
    """Result of a digest lookup, distinguishing 'disabled' from real errors."""

    status: DigestStatus
    digest: Optional[str] = None


class ArgoCDClient:
    """Client for interacting with ArgoCD CLI to get running image digests."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._logged_in = False

    def _is_token_expired_error(self, stderr: str) -> bool:
        """Detect whether a CLI error indicates an expired or invalid session."""
        lowered = stderr.lower()

        return any(
            indicator in lowered
            for indicator in _TOKEN_EXPIRED_INDICATORS
        )

    def _login(self) -> bool:
        """Perform ArgoCD login via CLI."""
        cmd = [
            "argocd",
            "login",
            self._config.argocd_server,
            "--insecure",
        ]

        if self._config.argocd_grpc_web:
            cmd.append("--grpc-web")

        if self._config.argocd_auth_token:
            cmd.extend(
                [
                    "--auth-token",
                    self._config.argocd_auth_token,
                ]
            )
            auth_method = "token"
        else:
            cmd.extend(
                [
                    "--username",
                    self._config.argocd_username,
                    "--password",
                    self._config.argocd_password,
                ]
            )
            auth_method = "username/password"

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.error(
                    "ArgoCD login failed (%s): %s",
                    auth_method,
                    result.stderr.strip(),
                )
                return False

            self._logged_in = True

            logger.info(
                "Successfully logged into ArgoCD at %s via %s",
                self._config.argocd_server,
                auth_method,
            )

            return True

        except subprocess.TimeoutExpired:
            logger.error("ArgoCD login timed out")
            return False

        except FileNotFoundError:
            logger.error(
                "ArgoCD CLI not found. "
                "Ensure 'argocd' is installed and in PATH"
            )
            return False

    def _invalidate_session(self) -> None:
        """Reset login state so the next operation triggers a fresh login."""
        self._logged_in = False

    def _ensure_logged_in(self) -> bool:
        """Ensure we are logged into ArgoCD."""
        if self._logged_in:
            return True

        return self._login()

    def _run_command(self, cmd: list[str], timeout: int = 60, retry_on_auth_error: bool = True) -> Optional[str]:
        """
        Execute an ArgoCD CLI command.

        If the current session has expired, invalidate it, login again
        and retry the command once.

        Returns stdout on success, None on failure.
        """
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            if result.returncode == 0:
                return result.stdout

            stderr = result.stderr.strip()

            # Session expired -> re-login and retry once.
            if (
                retry_on_auth_error
                and self._is_token_expired_error(stderr)
            ):
                logger.warning(
                    "ArgoCD session expired. "
                    "Re-authenticating and retrying command."
                )

                self._invalidate_session()

                if not self._login():
                    return None

                return self._run_command(
                    cmd,
                    timeout=timeout,
                    retry_on_auth_error=False,
                )

            logger.error(
                "ArgoCD command failed: %s",
                stderr,
            )
            return None

        except subprocess.TimeoutExpired:
            logger.error("ArgoCD command timed out")
            return None

        except FileNotFoundError:
            logger.error("ArgoCD CLI not found")
            return None

    def _get_pods(self, app_name: str) -> list[dict]:
        """
        Get all Pods belonging to an ArgoCD application.
        """
        cmd = ["argocd", "app", "get-resource", app_name, "--kind", "Pod", "--output", "json"]

        if self._config.argocd_grpc_web:
            cmd.append("--grpc-web")

        stdout = self._run_command(cmd)

        if stdout is None:
            return []

        try:
            documents = list(yaml.safe_load_all(stdout))

        except yaml.YAMLError:
            logger.exception(
                "Failed to parse ArgoCD Pod resources for %s",
                app_name,
            )
            return []

        pods: list[dict] = []

        for document in documents:
            if not isinstance(document, dict):
                continue

            if document.get("kind") != "Pod":
                continue

            pods.append(document)

        logger.debug(
            "Found %d Pod(s) for ArgoCD application %s",
            len(pods),
            app_name,
        )

        return pods

    def _extract_running_digest(
        self,
        pod: dict,
        target: DeploymentTarget,
    ) -> Optional[str]:
        """
        Extract the digest of the running container matching image_pattern.

        Matching is performed against both:
            status.containerStatuses[].image
            status.containerStatuses[].imageID

        The digest itself is always extracted from imageID.
        """
        pod_name = pod.get("metadata", {}).get("name", "<unknown>")

        status = pod.get("status", {})
        container_statuses = status.get("containerStatuses", [])

        for container in container_statuses:
            image = container.get("image", "")
            image_id = container.get("imageID", "")

            # imageID is the most reliable source because it contains:
            #
            # registry/repository@sha256:<digest>
            #
            # while image may contain a tag such as :latest, :oidc,
            # or :v1.0.0.
            image_matches = target.image_pattern in image
            image_id_matches = target.image_pattern in image_id

            if not image_matches and not image_id_matches:
                continue

            match = DIGEST_PATTERN.search(image_id)

            if not match:
                logger.warning(
                    "Container %s in Pod %s matches image pattern %s "
                    "but imageID does not contain a valid digest: %s",
                    container.get("name"),
                    pod_name,
                    target.image_pattern,
                    image_id,
                )
                continue

            # group(1) is the bare "sha256:<hash>" without the leading "@",
            # matching the format ECR returns so digest comparison works.
            digest = match.group(1)

            logger.info(
                "ArgoCD running digest for %s [%s] -> %s",
                target.app_name,
                target.image_pattern,
                digest,
            )

            return digest

        return None

    def _find_running_digest(self, app_name: str, target: DeploymentTarget) -> DigestResult:
        """
        Find the running digest by inspecting Pods belonging to the ArgoCD app.
        """
        pods = self._get_pods(app_name)

        if not pods:
            logger.info(
                "No Pods found for %s - treating as not deployed",
                app_name,
            )
            return DigestResult(DigestStatus.NOT_DEPLOYED)

        for pod in pods:
            pod_name = pod.get("metadata", {}).get("name", "<unknown>")
            phase = pod.get("status", {}).get("phase")

            if phase != "Running":
                logger.debug(
                    "Skipping Pod %s because phase is %s",
                    pod_name,
                    phase,
                )
                continue

            digest = self._extract_running_digest(
                pod,
                target,
            )

            if digest:
                return DigestResult(
                    DigestStatus.FOUND,
                    digest,
                )

        logger.info(
            "No running Pod with image matching pattern '%s' "
            "found for %s - treating as not deployed",
            target.image_pattern,
            app_name,
        )

        return DigestResult(DigestStatus.NOT_DEPLOYED)

    def get_running_digest(
        self,
        target: DeploymentTarget,
    ) -> DigestResult:
        """
        Get the actual running image digest through ArgoCD.

        Flow:

        1. Login to ArgoCD.
        2. Get all Pods belonging to the ArgoCD Application.
        3. Keep only Running Pods.
        4. Match the configured image.
        5. Read status.containerStatuses[].imageID.
        6. Return the SHA256 digest.

        NOT_DEPLOYED means that no matching running Pod is currently
        available.

        ERROR means that ArgoCD communication/authentication failed.
        """
        if not self._ensure_logged_in():
            return DigestResult(DigestStatus.ERROR)

        return self._find_running_digest(target.app_name, target)