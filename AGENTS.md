# AGENTS.md — video-processing

Short working rules for AI agents in THIS repository. Simple English;
technical terms are explained where they are first needed.

## What this repo is

This repo owns the **AWS video pipeline** — the code that turns an uploaded
video into watermarked, DRM-protected HLS/DASH renditions on a GPU worker:
Python packages (`src/`), Docker images (`deploy/`), Terraform (`infra/`),
the Bunny edge script (`edge/bunny/`), Packer AMI recipes, configs and tests.
It does NOT contain the Go backend; that lives in the separate `video-guard`
repository. This repo is also checked out as a git submodule of `video-guard`
at `video-processing/`; these rules and this repo's root CI are
unchanged. It has one nested git submodule of its own: `forensic-wm/`
(public HTTPS, pinned in `.gitmodules`).

**The declared source of truth is `CONTRACT.md`.** Every file name, message,
key and flag in it is a contract: do not invent your own. If an
implementation seems to deviate from CONTRACT.md, the contract wins — raise
the conflict instead of silently changing either side.

## Layout rule that matters most

`forensic-wm/` is a **nested git submodule** of this repo (public HTTPS,
pinned in `.gitmodules`); no SSH deploy key is needed. It holds the generator
research subtree and has its **own
`AGENTS.md`**, which applies ONLY inside `forensic-wm/`. The rules in this
root file govern everything OUTSIDE `forensic-wm/` (and the parts of
`forensic-wm/` its own file does not contradict).

## Working rules

- **Python 3.12, type hints, structured logging.** Every Python module uses
  type hints, pydantic-style contracts where inputs are checked, and
  structured (JSON) logging. Run tests before presenting work:
  `~/.pyenv/versions/3.12.0/bin/python3.12 -m pytest tests -q`
  (edge script: `deno test --allow-read edge/bunny/tests/`).
- **No commits, no pushes, no `git add`.** Leave all changes uncommitted for
  the owner to review and push.
- **No secrets in tracked files or logs.** Secret values live in AWS Secrets
  Manager, GitHub secrets or Railway variables — never in source, configs,
  test fixtures or log output.
- **Deferred gates are never "done".** CONTRACT.md §11 lists everything that
  was NOT verified locally (no real AWS, no GPU, no terraform/packer binary,
  no Shaka, no Axinom, no real Bunny runtime, no ClamAV image). Never claim
  any of those ran — not in code comments, docs, commit messages or reports.
- **Simple English in docs.** Explain a technical term the first time it
  appears.
