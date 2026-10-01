"""
Session Management and Security Isolation Service.
Satisfies R1 and A7 requirements.
"""

import asyncio
import inspect
import json
import logging
import os
import re
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import jwt
import requests

from app.config import settings
from app.models.session import (
    ReconnectFlowStatus,
    ReconnectWizardStatus,
    Session,
    SessionCredentials,
    SessionStatus,
    SessionStatusResponse,
    SessionUser,
)
from app.persistence.session_repo import SessionRepo

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class SessionService:
    """
    Manages session lifecycle, credential ingestion, upstream token verification,
    filesystem permission enforcement, and interactive reconnection.
    """

    def __init__(
        self,
        session_repo: Optional[Any] = None,
        session_dir: Optional[Union[str, Path]] = None,
        db_path: Optional[Union[str, Path]] = None,
        base_url: Optional[str] = None,
        user_agent: str = DEFAULT_USER_AGENT,
        http_client: Optional[Any] = None,
    ):
        self.session_repo = session_repo or SessionRepo()
        self.session_dir = Path(session_dir or settings.full_session_data_dir)
        self.db_path = Path(db_path or settings.db_path)
        self.base_url = (base_url or settings.UPSTREAM_BASE_URL).rstrip("/")
        self.user_agent = user_agent
        self.http_client = http_client

        # Reconnect wizard and circuit breaker state
        self.refresh_attempt_count = 0
        self._refresh_lock: Optional[asyncio.Lock] = None
        self.active_wizard_task: Optional[asyncio.Task] = None
        self.wizard_start_time: float = 0.0
        self.wizard_status: ReconnectWizardStatus = ReconnectWizardStatus.IDLE
        self.wizard_message: str = "Wizard is idle."

        # Enforce filesystem security at initialization
        self.enforce_storage_security()

    def enforce_storage_security(self) -> None:
        """Enforces POSIX 0700 on session data directory and 0600 on SQLite DB and WAL files."""
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                try:
                    os.chmod(self.session_dir, 0o700)
                except OSError as e:
                    logger.warning(f"Could not enforce 0700 on {self.session_dir}: {e}")

                if self.db_path.exists():
                    try:
                        os.chmod(self.db_path, 0o600)
                    except OSError as e:
                        logger.warning(f"Could not enforce 0600 on {self.db_path}: {e}")

                wal = Path(f"{self.db_path}-wal")
                shm = Path(f"{self.db_path}-shm")
                if wal.exists():
                    try:
                        os.chmod(wal, 0o600)
                    except OSError:
                        pass
                if shm.exists():
                    try:
                        os.chmod(shm, 0o600)
                    except OSError:
                        pass
        except Exception as e:
            logger.error(f"Failed to enforce storage security permissions: {e}")

    def verify_git_exclusion(self, repo_root: Optional[Path] = None) -> bool:
        """Verifies that .gitignore strictly excludes .session_data and *.db."""
        root = repo_root or settings.BASE_DIR
        gitignore = root / ".gitignore"
        required_patterns = [r"\.session_data", r"\*\.db", r"\*\.sqlite"]
        if not gitignore.exists():
            return False
        content = gitignore.read_text(encoding="utf-8")
        lines = content.splitlines()
        for pat in required_patterns:
            if not any(re.search(pat, line) for line in lines):
                return False
        return True

    verify_git_isolation = verify_git_exclusion

    @staticmethod
    def reassemble_cookie_chunks(cookies: Dict[str, str], base_key: str) -> Optional[str]:
        """
        Detects chunked cookies (${base_key}.0, ${base_key}.1, ...) and reassembles them
        into the full token value in correct numerical sequence.
        """
        pattern = re.compile(rf"^{re.escape(base_key)}\.(\d+)$")
        chunks: List[Tuple[int, str]] = []
        for k, v in cookies.items():
            m = pattern.match(k)
            if m:
                chunks.append((int(m.group(1)), v))
        if not chunks:
            return None
        # Sort numerically by chunk index (e.g. 0, 1, 2... 10)
        chunks.sort(key=lambda x: x[0])
        return "".join(v for _, v in chunks)

    @staticmethod
    def parse_cookie_input(raw: Union[str, dict, list]) -> Dict[str, str]:
        """
        Parses cookies from header strings, JSON arrays, dictionaries, or Netscape format.
        Normalizes all formats into a clean key-value mapping.
        """
        cookies: Dict[str, str] = {}
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}

        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and "name" in item and "value" in item:
                    cookies[str(item["name"])] = str(item["value"])
            return cookies

        raw_str = str(raw).strip()
        if raw_str.startswith("Cookie:"):
            raw_str = raw_str[len("Cookie:"):].strip()

        # Handle JSON strings
        if raw_str.startswith("[") or raw_str.startswith("{"):
            try:
                parsed = json.loads(raw_str)
                return SessionService.parse_cookie_input(parsed)
            except Exception:
                pass

        # Handle line-based (Netscape format or multiple lines)
        for line in raw_str.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                parts = line.split("\t")
                if len(parts) >= 7:
                    cookies[parts[5]] = parts[6]
                    continue
            for part in line.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                        v = v[1:-1]
                    if k:
                        cookies[k] = v
        return cookies

    @staticmethod
    def decode_jwt_claims(access_token: str) -> Dict[str, Any]:
        """Safely decodes JWT claims without signature verification, never leaking secrets."""
        try:
            return jwt.decode(access_token, options={"verify_signature": False})
        except Exception as e:
            logger.warning(f"Could not decode JWT claims: {e}")
            return {}

    async def _call_repo(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Helper to invoke synchronous or asynchronous repository methods."""
        res = func(*args, **kwargs)
        if inspect.isawaitable(res):
            return await res
        return res

    async def get_current_session_status(self) -> SessionStatusResponse:
        """
        Fast, zero-network cold recovery check (sub-20ms).
        Reads session from SQLite repo, checks expiration, and returns status.
        """
        session = await self._call_repo(self.session_repo.get_default)
        if not session or session.status == SessionStatus.MISSING or not session.access_token:
            return SessionStatusResponse(
                status=SessionStatus.MISSING,
                is_valid=False,
                is_authenticated=False,
                message="No session configured. Please import cookies or launch login wizard.",
                message_fr="Aucune session configurée. Veuillez importer les cookies ou lancer l'assistant de connexion.",
                action_required="RECONNECT",
                reconnect_guidance=(
                    "Paste your __Secure-next-auth.session-token cookie into the reconnect modal "
                    "or launch the interactive browser login wizard."
                ),
                guidance=[
                    "Option 1: Paste __Secure-next-auth.session-token cookie into the Reconnect Modal.",
                    "Option 2: Launch the interactive browser login wizard."
                ]
            )

        now = datetime.now(timezone.utc)
        if session.status == SessionStatus.EXPIRED or (session.expires_at and session.expires_at <= now):
            # Token is expired locally or upstream invalidated
            if session.status != SessionStatus.EXPIRED:
                await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
            return SessionStatusResponse(
                status=SessionStatus.EXPIRED,
                is_valid=False,
                is_authenticated=False,
                user_email=session.user_email,
                user_name=session.user_name,
                user_id=session.account_id,
                expires_at=session.expires_at,
                last_verified_at=session.last_verified_at,
                message="Session has expired. Automatic or manual reconnection required.",
                message_fr="La session a expiré. Une reconnexion automatique ou manuelle est requise.",
                action_required="RECONNECT",
                reconnect_guidance="Your session token has expired. Please re-authenticate or import fresh cookies.",
                guidance=[
                    "Click 'Reconnect' to refresh credentials or enter a fresh cookie."
                ]
            )

        is_valid = (session.status == SessionStatus.VALID)
        return SessionStatusResponse(
            status=session.status,
            is_valid=is_valid,
            is_authenticated=is_valid,
            user_email=session.user_email,
            user_name=session.user_name,
            user_id=session.account_id,
            expires_at=session.expires_at,
            last_verified_at=session.last_verified_at,
            message="Session is valid and active." if is_valid else f"Session state is {session.status}.",
            message_fr="Session valide et active." if is_valid else f"État de session: {session.status}."
        )

    def get_current_session_status_sync(self) -> SessionStatusResponse:
        """Synchronous wrapper for get_current_session_status."""
        return asyncio.run(self.get_current_session_status())

    async def _execute_http_get(self, url: str, headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        """Executes GET request using injected client or requests fallback."""
        if self.http_client is not None:
            # Allow mock or custom client
            if hasattr(self.http_client, "get"):
                resp = self.http_client.get(url, headers=headers)
                if inspect.isawaitable(resp):
                    resp = await resp
                status_code = getattr(resp, "status_code", 200)
                data = resp.json() if callable(getattr(resp, "json", None)) else {}
                return status_code, data

        def _sync_request() -> Tuple[int, Dict[str, Any]]:
            try:
                if url.startswith("https://"):
                    try:
                        from curl_cffi import requests as cffi_requests
                        r = cffi_requests.get(url, headers=headers, impersonate="chrome124", timeout=15.0)
                        try:
                            body = r.json()
                        except Exception:
                            body = {}
                        return r.status_code, body
                    except Exception as cffi_err:
                        logger.debug(f"curl_cffi get failed, falling back to requests: {cffi_err}")
                r = requests.get(url, headers=headers, timeout=15.0)
                try:
                    body = r.json()
                except Exception:
                    body = {}
                return r.status_code, body
            except Exception as e:
                logger.error(f"Network error calling {url}: {e}")
                return 0, {"error": str(e)}

        return await asyncio.to_thread(_sync_request)

    async def import_session_cookies(self, cookies_raw: Union[str, dict, list]) -> SessionStatusResponse:
        """
        Parses raw cookies, verifies against /api/auth/session, and persists session.
        Enforces 1-refresh circuit breaker and safe secret isolation.
        """
        cookies = self.parse_cookie_input(cookies_raw)

        # 1. Detect and reassemble NextAuth chunked cookies (.0, .1, ...)
        for base_key in ("__Secure-next-auth.session-token", "next-auth.session-token"):
            if base_key not in cookies or not cookies[base_key]:
                reassembled = self.reassemble_cookie_chunks(cookies, base_key)
                if reassembled:
                    cookies[base_key] = reassembled

        token_key = "__Secure-next-auth.session-token"
        if token_key not in cookies or not cookies[token_key]:
            if "next-auth.session-token" in cookies and cookies["next-auth.session-token"]:
                token_key = "next-auth.session-token"
            else:
                return SessionStatusResponse(
                    status=SessionStatus.MISSING,
                    is_valid=False,
                    is_authenticated=False,
                    message="Missing required session token cookie: '__Secure-next-auth.session-token'.",
                    message_fr="Cookie de session '__Secure-next-auth.session-token' manquant.",
                    action_required="RECONNECT",
                    reconnect_guidance="Cookie '__Secure-next-auth.session-token' was not found in the input.",
                    guidance=[
                        "Ensure you copy cookies from chatgpt.com via Developer Tools (F12 -> Application/Storage -> Cookies)."
                    ]
                )

        device_id = cookies.get("oai-device-id") or str(uuid.uuid4())
        cookies["oai-device-id"] = device_id

        # Verify against upstream session endpoint
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Cookie": cookie_header,
            "Referer": f"{self.base_url}/",
        }

        url = f"{self.base_url}/api/auth/session"
        status_code, data = await self._execute_http_get(url, headers)

        # Defensive check: ensure data is a dictionary
        if not isinstance(data, dict):
            data = {}

        if status_code == 403:
            # DIRECT_HTTP was challenged by Cloudflare Turnstile.
            # Attempt BROWSER_FETCH fallback from the local Chromium profile if available!
            chromium_profile = self.session_dir / "chromium"
            if chromium_profile.exists() and (self.base_url.startswith("https://") or "chatgpt.com" in self.base_url):
                logger.info("DIRECT_HTTP 403 encountered; attempting BROWSER_FETCH fallback via Chromium profile...")
                try:
                    bf_res = await self.sync_from_browser_profile()
                    if bf_res.is_valid or bf_res.status == SessionStatus.VALID:
                        return bf_res
                except Exception as bf_err:
                    logger.warning(f"BROWSER_FETCH fallback error: {bf_err}")

            existing = await self._call_repo(self.session_repo.get_default)
            if existing:
                await self._call_repo(self.session_repo.update_status, SessionStatus.CHALLENGE_REQUIRED)
            else:
                challenge_sess = Session(
                    id="default",
                    cookies_json=json.dumps(cookies),
                    user_agent=self.user_agent,
                    device_id=device_id,
                    status=SessionStatus.CHALLENGE_REQUIRED,
                )
                await self._call_repo(self.session_repo.save, challenge_sess)
            return SessionStatusResponse(
                status=SessionStatus.CHALLENGE_REQUIRED,
                is_valid=False,
                is_authenticated=False,
                message="Cloudflare Turnstile challenge detected (HTTP 403). Interactive browser verification required.",
                message_fr="Défi Cloudflare Turnstile détecté (HTTP 403). Vérification par navigateur requise.",
                action_required="SOLVE_CHALLENGE",
                reconnect_guidance="Cloudflare challenge encountered. Please launch the interactive browser login."
            )

        if status_code != 200:
            existing = await self._call_repo(self.session_repo.get_default)
            if existing and existing.status != SessionStatus.EXPIRED:
                await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
            return SessionStatusResponse(
                status=SessionStatus.EXPIRED,
                is_valid=False,
                is_authenticated=False,
                message=f"Session rejected by server with HTTP {status_code}.",
                message_fr=f"Session rejetée par le serveur (HTTP {status_code}).",
                action_required="RECONNECT",
                reconnect_guidance=f"Authentication server rejected session credentials with code {status_code}."
            )

        return await self._persist_session_data(data, cookies, device_id)

    async def _persist_session_data(
        self, data: Dict[str, Any], cookies: Dict[str, str], device_id: str
    ) -> SessionStatusResponse:
        """Persists valid NextAuth session data to SQLite and resets circuit breaker."""
        access_token = data.get("accessToken")
        if not access_token:
            # NextAuth frequently returns HTTP 200 with {} or {"expires": ...} on unauthenticated requests
            existing = await self._call_repo(self.session_repo.get_default)
            if existing and existing.status != SessionStatus.EXPIRED:
                await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
            return SessionStatusResponse(
                status=SessionStatus.EXPIRED,
                is_valid=False,
                is_authenticated=False,
                message="Session cookie is recognized but returned an empty access token. Session has expired.",
                message_fr="Cookie reconnu mais jeton d'accès vide. La session a expiré.",
                action_required="RECONNECT",
                reconnect_guidance="Upstream returned no active access token. Please re-login to chatgpt.com."
            )

        # Decode JWT claims
        claims = self.decode_jwt_claims(access_token)
        exp_ts = claims.get("exp")
        now = datetime.now(timezone.utc)
        if exp_ts:
            expires_at = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
        elif data.get("expires"):
            try:
                expires_at = datetime.fromisoformat(data["expires"].replace("Z", "+00:00"))
            except Exception:
                expires_at = None
        else:
            expires_at = None

        user_info = data.get("user") or {}
        auth_claims = claims.get("https://api.openai.com/auth") or {}
        user_email = user_info.get("email") or claims.get("email") or auth_claims.get("email")
        user_name = user_info.get("name")
        user_id = user_info.get("id") or claims.get("sub") or auth_claims.get("user_id")
        plan_type = auth_claims.get("plan_type", "unknown")

        # REJECT EXPIRED TOKENS: Do not save as VALID and do NOT reset circuit breaker
        if expires_at and expires_at <= now:
            existing = await self._call_repo(self.session_repo.get_default)
            if existing and existing.status != SessionStatus.EXPIRED:
                await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
            return SessionStatusResponse(
                status=SessionStatus.EXPIRED,
                is_valid=False,
                is_authenticated=False,
                user_email=user_email,
                user_name=user_name,
                user_id=user_id,
                plan_type=plan_type,
                expires_at=expires_at,
                last_verified_at=now,
                message="Access token has expired.",
                message_fr="Le jeton d'accès a expiré.",
                action_required="RECONNECT",
                reconnect_guidance="The session token retrieved from upstream has already expired. Please re-authenticate.",
                guidance=[
                    "Click 'Reconnect' to refresh credentials or enter a fresh cookie."
                ]
            )

        # Persist session to SQLite (only valid sessions reach here)
        session = Session(
            id="default",
            access_token=access_token,
            cookies_json=json.dumps(cookies),
            user_agent=self.user_agent,
            device_id=device_id,
            account_id=user_id,
            user_email=user_email,
            user_name=user_name,
            status=SessionStatus.VALID,
            expires_at=expires_at,
            last_verified_at=now,
        )
        await self._call_repo(self.session_repo.save, session)
        self.enforce_storage_security()
        self.refresh_attempt_count = 0  # Reset circuit breaker ONLY on valid token

        return SessionStatusResponse(
            status=SessionStatus.VALID,
            is_valid=True,
            is_authenticated=True,
            user_email=user_email,
            user_name=user_name,
            user_id=user_id,
            plan_type=plan_type,
            expires_at=expires_at,
            last_verified_at=now,
            message="Session successfully authenticated and stored.",
            message_fr="Session authentifiée et enregistrée avec succès."
        )

    async def get_valid_access_token(self) -> str:
        """
        Returns active access token for transports.
        Refreshes silently if token is expiring within 60 seconds.
        Raises RuntimeError if session is EXPIRED or MISSING.
        """
        session = await self._call_repo(self.session_repo.get_default)
        if not session or not session.access_token:
            raise RuntimeError("SESSION_MISSING: No session active. Please authenticate.")

        now = datetime.now(timezone.utc)
        if session.expires_at and (session.expires_at - now).total_seconds() < 60:
            if self._refresh_lock is None:
                self._refresh_lock = asyncio.Lock()
            async with self._refresh_lock:
                refreshed = await self._call_repo(self.session_repo.get_default)
                if (
                    refreshed
                    and refreshed.access_token
                    and refreshed.status == SessionStatus.VALID
                    and refreshed.expires_at
                    and (refreshed.expires_at - datetime.now(timezone.utc)).total_seconds() >= 60
                ):
                    return refreshed.access_token

                if self.refresh_attempt_count < 1:
                    self.refresh_attempt_count += 1
                    cookies = session.cookies
                    res = await self.import_session_cookies(json.dumps(cookies))
                    if res.is_valid:
                        fresh_session = await self._call_repo(self.session_repo.get_default)
                        if fresh_session and fresh_session.access_token:
                            return fresh_session.access_token

                # Update SQLite status to EXPIRED before raising to prevent split-brain
                await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
                raise RuntimeError("SESSION_EXPIRED: Access token expired and automatic refresh failed.")

        if session.status != SessionStatus.VALID:
            raise RuntimeError(f"SESSION_{session.status}: Session is not in VALID state.")

        return session.access_token

    async def start_reconnect_flow(self, force_restart: bool = True) -> ReconnectFlowStatus:
        """Launches interactive browser login wizard bounded by 300s timeout."""
        if self.active_wizard_task and not self.active_wizard_task.done():
            if force_restart:
                logger.info("Cancelling existing login wizard to launch a fresh one.")
                await self.cancel_reconnect_flow()
            else:
                elapsed = int(time.time() - self.wizard_start_time)
                return ReconnectFlowStatus(
                    wizard_status=ReconnectWizardStatus.RUNNING,
                    status="RUNNING",
                    elapsed_seconds=elapsed,
                    timeout_seconds=settings.RECONNECT_TIMEOUT_SECONDS,
                    message="Browser login wizard is already running.",
                    session_status=SessionStatus.MISSING,
                )

        self.wizard_start_time = time.time()
        self.wizard_status = ReconnectWizardStatus.RUNNING
        self.wizard_message = "Ouverture du navigateur Chromium... Veuillez vous connecter."
        self.active_wizard_task = asyncio.create_task(self._run_browser_login_wizard())

        return ReconnectFlowStatus(
            wizard_status=ReconnectWizardStatus.RUNNING,
            status="RUNNING",
            elapsed_seconds=0,
            timeout_seconds=settings.RECONNECT_TIMEOUT_SECONDS,
            message=self.wizard_message,
            session_status=SessionStatus.MISSING,
        )

    async def _run_browser_login_wizard(self) -> None:
        """Background browser wizard task bounded by 300 seconds timeout."""
        chromium_profile = self.session_dir / "chromium"
        chromium_profile.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(chromium_profile, 0o700)
            except OSError:
                pass

        playwright = None
        context = None
        self._current_wizard_context = None
        try:
            from playwright.async_api import async_playwright
            playwright = await async_playwright().start()
            self._current_wizard_playwright = playwright

            # In interactive login wizard, headed mode is required so user sees ChatGPT login page
            has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
            use_headless = not has_display

            # Ensure keyboard layout is set to French (AZERTY) on X11 / Wayland
            if has_display and os.name == "posix":
                try:
                    import subprocess
                    subprocess.run(["setxkbmap", "fr"], capture_output=True, check=False)
                except Exception as e:
                    logger.debug(f"Could not run setxkbmap fr: {e}")

            logger.info(
                f"Launching login wizard Chromium (headless={use_headless}, display={os.environ.get('DISPLAY')})..."
            )

            # Use Playwright's verified bundled Chromium (no snap wrapper)
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(chromium_profile),
                headless=use_headless,
                locale="fr-FR",
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1280,900",
                    "--lang=fr-FR",
                ],
            )
            self._current_wizard_context = context
            page = context.pages[0] if context.pages else await context.new_page()

            logger.info(f"Navigating to {self.base_url}...")
            await page.goto(f"{self.base_url}/", timeout=60000)

            start_t = time.time()
            timeout_limit = settings.RECONNECT_TIMEOUT_SECONDS
            last_seen_cookies: List[Dict[str, Any]] = []

            while time.time() - start_t < timeout_limit:
                await asyncio.sleep(1.5)

                # Check if window or context was closed by user
                try:
                    if not context.pages or page.is_closed():
                        token_cookie = next(
                            (c for c in last_seen_cookies if c["name"].startswith("__Secure-next-auth.session-token") or c["name"].startswith("next-auth.session-token")),
                            None
                        )
                        if token_cookie:
                            cookies_dict = {c["name"]: c["value"] for c in last_seen_cookies}
                            res = await self.import_session_cookies(cookies_dict)
                            if res.is_valid or res.status == SessionStatus.VALID:
                                self.wizard_status = ReconnectWizardStatus.SUCCESS
                                self.wizard_message = "Authentification réussie ! Session enregistrée."
                                return
                        self.wizard_status = ReconnectWizardStatus.CANCELLED
                        self.wizard_message = "Fenêtre du navigateur fermée par l'utilisateur."
                        return

                    cookies_list = await context.cookies()
                    last_seen_cookies = cookies_list
                except Exception:
                    token_cookie = next(
                        (c for c in last_seen_cookies if c["name"].startswith("__Secure-next-auth.session-token") or c["name"].startswith("next-auth.session-token")),
                        None
                    )
                    if token_cookie:
                        cookies_dict = {c["name"]: c["value"] for c in last_seen_cookies}
                        res = await self.import_session_cookies(cookies_dict)
                        if res.is_valid or res.status == SessionStatus.VALID:
                            self.wizard_status = ReconnectWizardStatus.SUCCESS
                            self.wizard_message = "Authentification réussie ! Session enregistrée."
                            return
                    self.wizard_status = ReconnectWizardStatus.CANCELLED
                    self.wizard_message = "Fenêtre du navigateur fermée."
                    return

                token_cookie = next(
                    (c for c in cookies_list if c["name"].startswith("__Secure-next-auth.session-token") or c["name"].startswith("next-auth.session-token")),
                    None
                )
                if token_cookie:
                    cookies_dict = {c["name"]: c["value"] for c in cookies_list}
                    try:
                        session_json = await page.evaluate(
                            '() => fetch("/api/auth/session").then(r => r.json()).catch(() => ({}))'
                        )
                        if isinstance(session_json, dict) and session_json.get("accessToken"):
                            device_id = cookies_dict.get("oai-device-id") or str(uuid.uuid4())
                            res = await self._persist_session_data(session_json, cookies_dict, device_id)
                            if res.is_valid or res.status == SessionStatus.VALID:
                                self.wizard_status = ReconnectWizardStatus.SUCCESS
                                self.wizard_message = "Authentification réussie ! Session enregistrée."
                                await asyncio.sleep(1.0)
                                return
                    except Exception:
                        pass

                    res = await self.import_session_cookies(cookies_dict)
                    if res.is_valid or res.status == SessionStatus.VALID:
                        self.wizard_status = ReconnectWizardStatus.SUCCESS
                        self.wizard_message = "Authentification réussie ! Session enregistrée."
                        await asyncio.sleep(1.0)
                        return

            self.wizard_status = ReconnectWizardStatus.TIMEOUT
            self.wizard_message = f"Délai d'attente dépassé ({timeout_limit}s)."
        except asyncio.CancelledError:
            self.wizard_status = ReconnectWizardStatus.CANCELLED
            self.wizard_message = "Assistant de reconnexion annulé."
        except Exception as e:
            logger.error(f"Error in browser login wizard: {e}", exc_info=True)
            self.wizard_status = ReconnectWizardStatus.FAILED
            self.wizard_message = f"Erreur de l'assistant : {e}"
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            if playwright:
                try:
                    await playwright.stop()
                except Exception:
                    pass
            self._current_wizard_context = None
            self._current_wizard_playwright = None
            self.enforce_storage_security()

    async def cancel_reconnect_flow(self) -> ReconnectFlowStatus:
        """Cancels running browser wizard cleanly without lingering processes."""
        if hasattr(self, "_current_wizard_context") and self._current_wizard_context:
            try:
                await self._current_wizard_context.close()
            except Exception:
                pass
        if self.active_wizard_task and not self.active_wizard_task.done():
            self.active_wizard_task.cancel()
            try:
                await self.active_wizard_task
            except asyncio.CancelledError:
                pass
        self.wizard_status = ReconnectWizardStatus.CANCELLED
        self.wizard_message = "Wizard was cancelled by user."
        current_status = await self.get_current_session_status()
        return ReconnectFlowStatus(
            wizard_status=self.wizard_status,
            status=self.wizard_status.value,
            elapsed_seconds=0,
            timeout_seconds=settings.RECONNECT_TIMEOUT_SECONDS,
            message=self.wizard_message,
            session_status=current_status.status,
        )

    def get_reconnect_status(self) -> ReconnectFlowStatus:
        """Returns the current status of the reconnection wizard."""
        elapsed = int(time.time() - self.wizard_start_time) if self.wizard_start_time else 0
        return ReconnectFlowStatus(
            wizard_status=self.wizard_status,
            status=self.wizard_status.value if hasattr(self.wizard_status, "value") else str(self.wizard_status),
            elapsed_seconds=elapsed,
            timeout_seconds=settings.RECONNECT_TIMEOUT_SECONDS,
            message=self.wizard_message,
        )

    # Aliases for route compatibility
    async def get_status_response(self) -> SessionStatusResponse:
        return await self.get_current_session_status()

    async def start_reconnect_wizard(self) -> ReconnectFlowStatus:
        return await self.start_reconnect_flow()

    async def import_session_data(
        self,
        cookies_input: Union[str, dict, list],
        access_token: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> SessionStatusResponse:
        """Imports cookies and optional token/device_id."""
        return await self.import_session_cookies(cookies_input)

    async def sync_from_browser_profile(self) -> SessionStatusResponse:
        """Extracts cookies and session payload directly from the saved Chromium user profile directory."""
        chromium_profile = self.session_dir / "chromium"
        if not chromium_profile.exists():
            return await self.get_current_session_status()

        playwright = None
        context = None
        try:
            from playwright.async_api import async_playwright
            playwright = await async_playwright().start()
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(chromium_profile),
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            cookies_list = await context.cookies()
            cookies_dict = {c["name"]: c["value"] for c in cookies_list}

            # Reassemble chunked cookies if needed
            for base_key in ("__Secure-next-auth.session-token", "next-auth.session-token"):
                if base_key not in cookies_dict or not cookies_dict[base_key]:
                    reassembled = self.reassemble_cookie_chunks(cookies_dict, base_key)
                    if reassembled:
                        cookies_dict[base_key] = reassembled

            device_id = cookies_dict.get("oai-device-id") or str(uuid.uuid4())
            page = context.pages[0] if context.pages else await context.new_page()

            # Attempt to evaluate /api/auth/session via BROWSER_FETCH (bypasses Cloudflare)
            session_json = None
            try:
                await page.goto(f"{self.base_url}/", timeout=25000)
                session_json = await page.evaluate(
                    '() => fetch("/api/auth/session").then(r => r.json()).catch(e => ({}))'
                )
            except Exception as e:
                logger.debug(f"Evaluate in sync_from_browser_profile: {e}")

            await context.close()
            context = None
            await playwright.stop()
            playwright = None

            if isinstance(session_json, dict) and session_json.get("accessToken"):
                logger.info("Successfully fetched accessToken via BROWSER_FETCH in Chromium.")
                return await self._persist_session_data(session_json, cookies_dict, device_id)

            return await self.import_session_cookies(cookies_dict)
        except Exception as e:
            logger.error(f"Failed to sync from browser profile: {e}", exc_info=True)
            return await self.get_current_session_status()
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            if playwright:
                try:
                    await playwright.stop()
                except Exception:
                    pass
