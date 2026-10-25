import importlib.util
import sys
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parents[1] / ".claude" / "skills"
SCRIPTS_DIR = SKILLS_DIR / "email-ingest" / "scripts"


def load_script(name: str, scripts_dir: Path = SCRIPTS_DIR):
    """Import a skill script by file name (the scripts folder is not a package)."""
    if str(SCRIPTS_DIR) not in sys.path:  # every script imports the shared vaultlib from here
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(name, scripts_dir / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def vaultlib():
    return load_script("vaultlib")


@pytest.fixture(scope="session")
def msg_to_md(vaultlib):
    return load_script("msg_to_md")


@pytest.fixture(scope="session")
def pending(vaultlib):
    return load_script("pending")


@pytest.fixture(scope="session")
def lint_vault(vaultlib):
    return load_script("lint_vault")


@pytest.fixture(scope="session")
def prune(msg_to_md):
    return load_script("prune")


@pytest.fixture(scope="session")
def draft_to_msg(vaultlib):
    return load_script("draft_to_msg", SKILLS_DIR / "email-draft" / "scripts")


@pytest.fixture
def vault(tmp_path):
    """Empty vault skeleton: CLAUDE.md marker plus the folders the scripts expect."""
    (tmp_path / "CLAUDE.md").write_text("# schema\n", encoding="utf-8")
    for folder in ("00-inbox", "raw/emails", "wiki/threads", "wiki/people"):
        (tmp_path / folder).mkdir(parents=True)
    return tmp_path
