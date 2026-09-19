"""Central configuration for the screening system.

Configuration management rules:
- Defaults live here; every value is overridable via environment variables
  (prefix ``APP_``) or a project-root ``.env`` file (see ``.env.example``).
- No other module reads ``os.environ`` directly — they call ``get_settings()``.

Policy flags (read these before adding features):
- ``MOCK_MODE``: when True the system may use mock/demo data. Mock data must
  always live under ``data/mock_registry`` and be clearly labeled.
- Any registry/lookup that *simulates* a government database must be
  registered in the MOCK registry so the API can disclose it.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

BASE_DIR: Path = Path(__file__).resolve().parents[2]

try:  # optional at import time; config still works without it
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # pragma: no cover
    pass

APP_NAME: str = "AI-Based Fake Identity & Document Screening System"
APP_VERSION: str = "0.2.0"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings:
    """Runtime settings. Instantiate via ``get_settings()``."""

    def __init__(self) -> None:
        self.app_name: str = APP_NAME
        self.app_version: str = APP_VERSION
        self.env: str = os.getenv("APP_ENV", "dev")
        self.log_level: str = os.getenv("APP_LOG_LEVEL", "INFO").upper()

        # --- Policy -----------------------------------------------------
        # MOCK_MODE=True => system uses mock/demo data where real data is
        # unavailable. NEVER connect to a real government database.
        self.mock_mode: bool = _env_bool("APP_MOCK_MODE", True)

        # --- Database ---------------------------------------------------
        self.database_url: str = self._resolve_database_url()

        # --- CORS -------------------------------------------------------
        self.cors_origins: list[str] = _env_list(
            "APP_CORS_ORIGINS",
            ["http://localhost:5173", "http://127.0.0.1:5173"],
        )

        # --- Paths ------------------------------------------------------
        self.base_dir: Path = BASE_DIR
        self.data_dir: Path = BASE_DIR / "data"
        self.upload_dir: Path = self.data_dir / "uploads"
        self.mock_registry_dir: Path = self.data_dir / "mock_registry"
        self.logs_dir: Path = BASE_DIR / "logs"

        # Uploads > this size are rejected (bytes).
        self.max_upload_bytes: int = _env_int("APP_MAX_UPLOAD_BYTES", 10 * 1024 * 1024)

        # --- Pipeline ---------------------------------------------------
        # Failure policy: "skip" marks the failed stage as errored and
        # continues; "fail" aborts the whole screening run.
        self.stage_failure_policy: str = os.getenv("APP_STAGE_FAILURE_POLICY", "skip")

        # --- Risk bands (Step 8 risk engine; 0-100 score scale) ----------
        # CONFIGURABLE band ceilings: score <= low_max => LOW, <= medium_max
        # => MEDIUM, <= high_max => HIGH, else CRITICAL. Defaults follow the
        # spec example (24/49/74) but are policy, not fact — tune per
        # deployment via APP_RISK_BAND_* env vars.
        self.risk_band_low_max: int = _env_int("APP_RISK_BAND_LOW_MAX", 24)
        self.risk_band_medium_max: int = _env_int("APP_RISK_BAND_MEDIUM_MAX", 49)
        self.risk_band_high_max: int = _env_int("APP_RISK_BAND_HIGH_MAX", 74)
        # Documents expiring within this many days contribute risk.
        self.risk_expiring_soon_days: int = _env_int("APP_RISK_EXPIRING_SOON_DAYS", 90)

        # --- AI provider selection (the pluggable seam) ------------------
        # Step 4: real preprocessing + OCR ship as defaults. If their deps
        # are missing the registry falls back to honest placeholders.
        self.ai_preprocessing_provider: str = os.getenv("APP_AI_PREPROCESSING_PROVIDER", "cv2-preprocess")
        self.ai_ocr_provider: str = os.getenv("APP_AI_OCR_PROVIDER", "rapidocr")

        # --- OCR module (Step 4) ----------------------------------------
        self.ocr_engine: str = os.getenv("APP_OCR_ENGINE", "auto")  # auto|rapidocr|tesseract
        self.ocr_target_width: int = _env_int("APP_OCR_TARGET_WIDTH", 1400)
        self.ocr_enable_perspective: bool = _env_bool("APP_OCR_ENABLE_PERSPECTIVE", True)
        self.ocr_enable_denoise: bool = _env_bool("APP_OCR_ENABLE_DENOISE", True)
        self.ocr_enable_contrast: bool = _env_bool("APP_OCR_ENABLE_CONTRAST", True)
        # --- Tampering module (Step 6) -----------------------------------
        # Analysis resolution cap (pixels per side) — keeps CPU time honest.
        self.tampering_max_dimension: int = _env_int("APP_TAMPERING_MAX_DIMENSION", 3000)
        # Risk-score thresholds used by the verdict router (0-100 scale).
        self.tampering_suspicious_min: float = float(os.getenv("APP_TAMPERING_SUSPICIOUS_MIN", "20"))
        self.tampering_likely_min: float = float(os.getenv("APP_TAMPERING_LIKELY_MIN", "55"))
        # Step 5: real validation rule engine ships as the default; the
        # placeholder stays available via APP_AI_VALIDATION_PROVIDER.
        self.ai_validation_provider: str = os.getenv("APP_AI_VALIDATION_PROVIDER", "rule-engine")
        # Step 6: forensic tampering heuristics ship as the default.
        # Optional trained model selection ("none" until one is registered).
        self.ai_tampering_provider: str = os.getenv("APP_AI_TAMPERING_PROVIDER", "forensic-heuristics")
        self.ai_tampering_model: str = os.getenv("APP_AI_TAMPERING_MODEL", "none")
        # Step 7: real face verification ships as the default (YuNet + SFace,
        # fully offline, 1:1 verification only). The placeholder stays
        # available via APP_AI_FACE_PROVIDER=placeholder.
        self.ai_face_provider: str = os.getenv("APP_AI_FACE_PROVIDER", "cv-face-yunet-sface")
        # Embedding backend selection: "sface" (built-in) | custom registered
        # name | "none" (honest abstention).
        self.face_embedding_model: str = os.getenv("APP_FACE_EMBEDDING_MODEL", "sface")
        # Step 8: deterministic weighted-rules risk fusion ships as the
        # default. The placeholder stays available via
        # APP_AI_RISK_PROVIDER=placeholder.
        self.ai_risk_provider: str = os.getenv("APP_AI_RISK_PROVIDER", "weighted-rules-v2")

        # --- Retention ---------------------------------------------------
        self.retention_days: int = _env_int("APP_RETENTION_DAYS", 30)

        # --- Blockchain audit anchor (Step 9) -----------------------------
        # Local EVM chain (Hardhat node by default). The chain stores report
        # HASHES only — never PII. When unreachable, anchors honestly stay
        # 'pending'; the DB audit trail works regardless.
        self.chain_rpc_url: str = os.getenv("APP_CHAIN_RPC_URL", "http://127.0.0.1:8545")
        # Empty → resolved from blockchain/deployed.json (written by deploy.js).
        self.chain_contract_address: str = os.getenv("APP_CHAIN_CONTRACT_ADDRESS", "")
        # LOCAL TEST ACCOUNTS ONLY (Hardhat accounts are publicly known).
        # Empty → use the node's unlocked coinbase account.
        self.chain_private_key: str = os.getenv("APP_CHAIN_PRIVATE_KEY", "")

        # --- Permissioned ledger abstraction (Step 10) ---------------------
        # Which LedgerBackend the /api/audit endpoints use:
        #   local — in-process mock permissioned ledger (demo/dev; NOT
        #           tamper-evident across restarts; honest about it)
        #   evm   — the local EVM chain via AuditAnchor (Hardhat today;
        #           any EVM permissioned ledger tomorrow)
        #   auto  — EVM when reachable, otherwise the local mock (disclosed)
        # Swapping in Hyperledger Fabric later means implementing the same
        # three-method interface (append/get/verify) — nothing else changes.
        self.ledger_backend: str = os.getenv("APP_LEDGER_BACKEND", "auto")

        # --- Security (Step 11) -------------------------------------------
        # SECRET KEY: signs JWTs. REQUIRED in prod — the app refuses to start
        # with APP_ENV=prod and no real secret. Never commit real keys.
        self.secret_key: str = os.getenv("APP_SECRET_KEY", "")
        self.access_token_expire_minutes: int = _env_int(
            "APP_ACCESS_TOKEN_EXPIRE_MINUTES", 30
        )
        # JWT issuer/audience — embedded in every token and verified on read.
        self.jwt_issuer: str = os.getenv("APP_JWT_ISSUER", "document-screening-system")
        self.jwt_audience: str = os.getenv("APP_JWT_AUDIENCE", "screening-dashboard")

        # Bootstrapping: on first startup with an empty users table, an ADMIN
        # is created from these credentials. The bootstrap password is read
        # from the environment ONLY (never a hardcoded default) — deployers
        # MUST set APP_BOOTSTRAP_ADMIN_PASSWORD or seeding is skipped with a
        # loud warning. Bootstrap login is forced to expire immediately.
        self.bootstrap_admin_username: str = os.getenv("APP_BOOTSTRAP_ADMIN_USERNAME", "admin")
        self.bootstrap_admin_password: str = os.getenv("APP_BOOTSTRAP_ADMIN_PASSWORD", "")

        # Account lockout: brute-force resistance without third-party deps.
        self.max_failed_logins: int = _env_int("APP_MAX_FAILED_LOGINS", 5)
        self.lockout_minutes: int = _env_int("APP_LOCKOUT_MINUTES", 15)

        # Upload cleanup: upload dirs untouched for this long are deleted.
        self.upload_retention_hours: int = _env_int("APP_UPLOAD_RETENTION_HOURS", 24)

        # Encryption at rest for sensitive columns (report_json carries OCR
        # fields, MRZ, names). APP_ENCRYPTION_KEY = Fernet key (urlsafe b64).
        # Empty in dev → loud warning, column stays plaintext (dev only).
        self.encryption_key: str = os.getenv("APP_ENCRYPTION_KEY", "")
        self.encryption_required_prod: bool = _env_bool("APP_ENCRYPTION_REQUIRED", True)

        # Rate limiting (slowapi): per-IP, in-process memory storage.
        self.rate_limit_default: str = os.getenv("APP_RATE_LIMIT_DEFAULT", "120/minute")
        self.rate_limit_auth: str = os.getenv("APP_RATE_LIMIT_AUTH", "10/minute")
        self.rate_limit_upload: str = os.getenv("APP_RATE_LIMIT_UPLOAD", "30/minute")
        self.rate_limit_analyze: str = os.getenv("APP_RATE_LIMIT_ANALYZE", "20/minute")

        # Production guardrails: refuse to boot insecure prod configs.
        # (Dev/test keep working without secrets so the prototype stays runnable.)
        if self.env == "prod":
            if not self.secret_key or len(self.secret_key) < 32:
                raise RuntimeError(
                    "APP_ENV=prod requires APP_SECRET_KEY (>= 32 chars). "
                    "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
                )
            if self.encryption_required_prod and not self.encryption_key:
                raise RuntimeError(
                    "APP_ENV=prod requires APP_ENCRYPTION_KEY (Fernet key). "
                    "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
                )

        self.ensure_dirs()

    def _resolve_database_url(self) -> str:
        url = os.getenv("APP_DB_URL")
        if url:
            return url
        # Default: project-local SQLite file (auto-created).
        return f"sqlite:///{(BASE_DIR / 'data' / 'app.db').as_posix()}"

    def ensure_dirs(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.mock_registry_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
