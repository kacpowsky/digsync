"""ArgoCD client for fetching running deployment digests.

Supports two auth strategies:
- Token: set ARGOCD_AUTH_TOKEN (e.g. from argocd account generate-token)
- Username/Password: set ARGOCD_USERNAME and ARGOCD_PASSWORD
"""

import logging
import re
import subprocess
from typing import Optional

from src.config import AppConfig, ArgoDeploymentTarget

logger = logging.getLogger(__name__)

DIGEST_PATTERN = re.compile(r"@(sha256:[a-f0-9]{64})")

_TOKEN_EXPIRED_INDICATORS = (
    "token is expired",
    "token has invalid claims",
    "invalid session",
    "Unauthenticated",
)


class ArgoCDClient:
    """Client for interacting with ArgoCD CLI to get running image digests."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._logged_in = False

    def _is_token_expired_error(self, stderr: str) -> bool:
        """Detect whether a CLI error indicates an expired or invalid session."""
        return any(indicator in stderr for indicator in _TOKEN_EXPIRED_INDICATORS)

    def _login(self) -> bool:
        """Perform ArgoCD login via CLI."""
        cmd = ["argocd", "login", self._config.argocd_server, "--insecure"]

        if self._config.argocd_grpc_web:
            cmd.append("--grpc-web")

        if self._config.argocd_auth_token:
            cmd.extend(["--auth-token", self._config.argocd_auth_token])
            auth_method = "token"
        else:
            cmd.extend([
                "--username", self._config.argocd_username,
                "--password", self._config.argocd_password,
            ])
            auth_method = "username/password"

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.error("ArgoCD login failed (%s): %s", auth_method, result.stderr.strip())
                return False

            self._logged_in = True
            logger.info("Successfully logged into ArgoCD at %s via %s", self._config.argocd_server, auth_method)
            return True

        except subprocess.TimeoutExpired:
            logger.error("ArgoCD login timed out")
            return False
        except FileNotFoundError:
            logger.error("ArgoCD CLI not found. Ensure 'argocd' is installed and in PATH")
            return False

    def _invalidate_session(self) -> None:
        """Reset login state so the next operation triggers a fresh login."""
        self._logged_in = False

    def _ensure_logged_in(self) -> bool:
        """Ensure we are logged into ArgoCD using token or username/password."""
        if self._logged_in:
            return True
        return self._login()

    def get_running_digest(self, target: ArgoDeploymentTarget) -> Optional[str]:
        """Get the currently running image digest for an ArgoCD application.

        Runs: argocd app manifests <app_name> [--grpc-web]
        Then extracts the digest from lines matching the image_pattern.

        If the session has expired, automatically re-authenticates and retries once.
        """
        if not self._ensure_logged_in():
            return None

        cmd = ["argocd", "app", "manifests", target.app_name]
        if self._config.argocd_grpc_web:
            cmd.append("--grpc-web")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()

                if self._is_token_expired_error(stderr):
                    logger.warning(
                        "Session expired while fetching manifests for %s, re-authenticating",
                        target.app_name,
                    )
                    self._invalidate_session()
                    if not self._login():
                        return None
                    return self._fetch_manifests(target)

                logger.error(
                    "Failed to get manifests for %s: %s",
                    target.app_name,
                    stderr,
                )
                return None

            return self._parse_digest(target, result.stdout)

        except subprocess.TimeoutExpired:
            logger.error("ArgoCD manifests command timed out for %s", target.app_name)
            return None
        except FileNotFoundError:
            logger.error("ArgoCD CLI not found")
            return None

    def _fetch_manifests(self, target: ArgoDeploymentTarget) -> Optional[str]:
        """Fetch manifests without retry (used after re-authentication)."""
        cmd = ["argocd", "app", "manifests", target.app_name]
        if self._config.argocd_grpc_web:
            cmd.append("--grpc-web")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                logger.error(
                    "Failed to get manifests for %s after re-login: %s",
                    target.app_name,
                    result.stderr.strip(),
                )
                return None

            return self._parse_digest(target, result.stdout)

        except subprocess.TimeoutExpired:
            logger.error("ArgoCD manifests command timed out for %s", target.app_name)
            return None
        except FileNotFoundError:
            logger.error("ArgoCD CLI not found")
            return None

    def _parse_digest(self, target: ArgoDeploymentTarget, stdout: str) -> Optional[str]:
        """Extract image digest from manifest output."""
        for line in stdout.splitlines():
            stripped = line.strip()
            if "image:" not in stripped:
                continue

            if target.image_pattern not in stripped:
                continue

            match = DIGEST_PATTERN.search(stripped)
            if match:
                digest = match.group(1)
                logger.info(
                    "ArgoCD running digest for %s [%s] -> %s",
                    target.app_name,
                    target.image_pattern,
                    digest,
                )
                return digest

        logger.warning(
            "No digest found in manifests for %s matching pattern '%s'",
            target.app_name,
            target.image_pattern,
        )
        return None
