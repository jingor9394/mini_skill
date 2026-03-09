## Privacy Policy

This privacy policy describes how **Mini Claw** (the “Plugin”) processes data when used in Dify.

### 1. Who We Are

- Author: lfenghx
- Repository: <https://github.com/lfenghx/mini_claw>
- Contact:
  - Email: 550916599@qq.com
  - GitHub: lfenghx
  - Bilibili: 元视界\_O凌枫o

### 2. Data We Process

The Plugin may process the following data to provide its functionality:

- **User input**: the `query` parameter and related conversation context passed by Dify.
- **Model selection/configuration**: the `model` selector and parameters used to call the LLM through Dify’s model runtime.
- **Generated artifacts**: files created in the Plugin’s temporary session directory during execution (e.g., `.txt`, `.md`, `.pdf`, images).
- **Uploaded files via Dify**: when Dify provides file URLs (e.g., workflow file inputs), the Plugin may download file contents into the session directory for processing.
- **Operational logs**: runtime debug logs used for troubleshooting. Logs may include tool call names, execution status, file paths under the session/temp directory, command arguments, and truncated stdout/stderr snippets.

The Plugin does **not** intentionally collect personal data beyond what is required to execute the user’s request.

### 3. How Data Is Used

Data is used strictly for:

- Selecting and invoking skills from the local `skills/` directory.
- Reading skill documentation (`SKILL.md`) and related skill files as needed.
- Running controlled commands inside controlled directories to generate deliverables, subject to the Plugin’s execution policies (e.g., allowlists, per-skill restrictions, and approval gates).
- Returning the final text and generated files back to Dify as tool outputs.
- Managing per-conversation persona and memory (identity/user/soul/memory) as part of the “soulful assistant” experience.

### 4. Data Sharing & Third Parties

Depending on your Dify configuration, data may be transmitted to:

- **LLM providers configured in Dify**: The Plugin invokes the LLM via Dify’s model runtime. Prompts and context may be sent to the configured provider to generate responses and tool plans.
- **Websites you explicitly ask the Plugin to access**: if a workflow or skill uses web fetching / web automation, requests may be made to third-party websites as part of fulfilling your request.

The Plugin itself does not add additional third-party analytics/telemetry services.

### 5. Storage & Retention

- **Temporary files**: The Plugin creates a per-run session directory under the plugin workspace (e.g., `temp/dify-skill-xxxx/`). These files are used to assemble deliverables and intermediate artifacts. The Plugin may automatically delete older session directories and keep only a small number of recent sessions.
- **Skill status cache**: the Plugin may write a skills snapshot cache file (e.g., `temp/skills_snapshot.json`) to speed up skill availability checks.
- **Conversation state (Dify storage)**: the Plugin may store state in Dify-provided storage to support multi-turn runs, including persona files (IDENTITY/USER/SOUL), memory files (MEMORY and daily memory), session directory pointers, and command approval decisions.

Retention is primarily controlled by:

- Your deployment environment (filesystem retention/backups)
- Your Dify configuration and storage lifecycle policies

### 6. Security

To reduce security risks:

- The Plugin restricts command execution via allowlists and per-skill constraints, and may require explicit user approval for some commands.
- File reads/writes are constrained to controlled directories (skill folders and the session/temp directories).
- Dependency installation is intended to be performed via the Skill Management tool; installation-like commands are restricted during normal “conversation execution”.

However, you should still treat generated files and logs as potentially sensitive if your inputs contain sensitive content.

### 7. Your Choices

- Avoid submitting sensitive personal information or secrets in `query` unless necessary.
- Manage/clear conversation data via Dify if your deployment requires data minimization.
- Remove plugin temp directories and/or clear Dify storage if you need immediate cleanup in self-hosted deployments.

### 8. Changes to This Policy

This policy may be updated as the Plugin evolves. Updates will be published in the repository.
