"""HONDANA application-wide constants."""

# LLM
LLM_MIN_CALL_INTERVAL: float = 4.0
LLM_SUMMARY_MAX_TOKENS: int = 512
RATE_LIMIT_PAUSE_SECONDS: int = 300

# Ingestion
EMBED_DIM_DEFAULT: int = 384
MIN_TEXT_LENGTH: int = 50
MIN_EXTRACT_CHARS: int = 200
INGEST_URLS_MAX: int = 50
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".md",
        ".html",
        ".htm",
        ".pdf",
        ".docx",
        ".pptx",
        ".txt",
        ".xlsx",
    }
)

# Jobs
JOB_CLEANUP_INTERVAL: int = 24 * 60 * 60

# Storage
DEFAULT_TABLE: str = "hondana_chunks"

# Answer generation
GENERATOR_MAX_TOKENS: int = 2048
