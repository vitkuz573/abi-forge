"""abi-forge generator SDK — bundled code generators for ABI-driven binding pipelines."""
from pathlib import Path

GENERATOR_SDK_PATH: Path = Path(__file__).resolve().parent
MANIFEST_PATH: str = str(GENERATOR_SDK_PATH / "plugin.manifest.json")
