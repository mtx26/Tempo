# Fresh Windows installation

1. In PowerShell:
   ```powershell
   winget install --id Git.Git -e
   winget install --id Microsoft.VisualStudioCode -e
   winget install --id Anthropic.ClaudeCode -e
   ```
   If the Claude package is unavailable, use the official Windows installer documented at https://code.claude.com/docs/en/setup (verify before running any downloaded command). Native installer often: `irm https://claude.ai/install.ps1 | iex`.
2. Restart terminal. Check `git --version`, `code --version`, `claude --version`.
3. Extract this ZIP to `C:\Projects\Tempo` so `CLAUDE.md` is at the repository root (avoid nested `Tempo\Tempo`).
4. Run `cd C:\Projects\Tempo`, `git init`, `code .`, `claude` and sign in.
5. Install **Superpowers** through Claude Code's `/plugin` interface, using its plugin marketplace/search and current official installation instructions; do not assume a hard-coded marketplace name. Confirm plugin actually installed. Optional: Context7 documentation plugin if available.
6. Start with `docs/BOOTSTRAP_PROMPT.md`; ask Claude to verify environment and present phase-0 plan first.
7. Do not enter secrets into chat or repository; configure later with safe secret storage.

Note: Claude Code uses its own Anthropic access/subscription; it is separate from the eventual Tempo LLM API.
