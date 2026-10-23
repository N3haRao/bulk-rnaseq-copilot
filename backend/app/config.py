"""Settings, read from environment variables with defaults for a local laptop."""

import os
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings:
    def __init__(self) -> None:
        # The R code bank (git submodule, pinned to a tagged commit)
        self.codebank_dir = Path(os.environ.get("CODEBANK_DIR", REPO_ROOT / "codebank")).resolve()
        # Uploads and analysis runs live here (not in git)
        self.data_dir = Path(os.environ.get("OMICS_DATA_DIR", REPO_ROOT / "data")).resolve()
        self.rscript = os.environ.get("RSCRIPT") or shutil.which("Rscript") or "Rscript"
        self.max_upload_mb = int(os.environ.get("MAX_UPLOAD_MB", "300"))
        # Built React app, served by the backend when present
        self.frontend_dist = Path(os.environ.get("FRONTEND_DIST", REPO_ROOT / "frontend" / "dist")).resolve()

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"


settings = Settings()
