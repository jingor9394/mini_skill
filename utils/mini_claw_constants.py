HISTORY_KEY_PREFIX = "skill:history:"
SESSION_DIR_KEY_PREFIX = "skill:session_dir:"
PERSONA_KEY_PREFIX = "skill:persona:"
MEMORY_KEY_PREFIX = "skill:memory:"
APPROVAL_KEY_PREFIX = "skill:approval:"

EXEC_ALLOWED_BINS = {
    "python",
    "pip",
    "node",
    "pandoc",
    "soffice",
    "pdftoppm",
    "npm",
    "npx",
    "bun",
    "curl",
    "uvx",
    "wget",
    "git",
    "bash",
    "uv",
    "cp",
    "mv",
    "ls",
}
EXEC_TRUSTED_DIR_PREFIXES = (
    "/usr/bin/",
    "/bin/",
    "/usr/local/bin/",
    "/usr/sbin/",
    "/sbin/",
    "/opt/",
)
TEMP_SESSION_PREFIX = "dify-skill-"
