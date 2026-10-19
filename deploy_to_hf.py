"""Upload Apni Awaaz to your Hugging Face Space.

Usage (from the project folder):
  pip install huggingface_hub
  python deploy_to_hf.py YOUR-USERNAME/apni-awaaz            upload
  python deploy_to_hf.py YOUR-USERNAME/apni-awaaz --dry-run  only list what would be sent

Only the files the Space needs are sent. Never .env (your keys), data/, certs/,
voice_test/ or outputs/. Keys go into the Space's secret settings instead (see README).
It asks for your Hugging Face access token (with write permission) and doesn't save it.
"""
import fnmatch
import os
import sys
from getpass import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# What the Space needs: its settings (README), the Dockerfile and the app itself
INCLUDE = ["Dockerfile", ".dockerignore", "README.md", "requirements.txt", "backend/*.py", "static/*"]
EXCLUDE = ["*__pycache__*", "*.pyc"]


def files_to_upload() -> list[str]:
    chosen = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if any(fnmatch.fnmatch(rel, p) for p in INCLUDE) and not any(fnmatch.fnmatch(rel, p) for p in EXCLUDE):
            chosen.append(rel)
    return chosen


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1 or "/" not in args[0]:
        sys.exit("Usage: python deploy_to_hf.py YOUR-USERNAME/SPACE-NAME [--dry-run]")
    space = args[0]

    files = files_to_upload()
    size = sum((ROOT / f).stat().st_size for f in files) / 1e6
    print(f"\nFiles for {space} ({len(files)} files, {size:.1f} MB):")
    for f in files:
        print("  ", f)
    for secret_file in (".env", "data/secret_key"):
        if secret_file in files:
            sys.exit(f"\nStopped: {secret_file} would be uploaded. It must never leave this computer.")
    if "--dry-run" in sys.argv:
        print("\nDry run: nothing was uploaded.")
        return

    if input("\nUpload these files? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return

    from huggingface_hub import HfApi

    token = os.environ.get("HF_TOKEN") or getpass("Hugging Face access token (input is hidden): ").strip()
    api = HfApi(token=token)
    print("Uploading…")
    commit = api.upload_folder(
        folder_path=str(ROOT),
        repo_id=space,
        repo_type="space",
        allow_patterns=INCLUDE,
        ignore_patterns=EXCLUDE,
        commit_message="Deploy Apni Awaaz",
    )
    owner, name = space.split("/")
    print(f"\nDone: {commit.commit_url}")
    print(f"The Space now builds (a few minutes). Watch it at https://huggingface.co/spaces/{space}")
    print(f"Your app's direct link: https://{owner.lower()}-{name.lower().replace('_', '-').replace('.', '-')}.hf.space")


if __name__ == "__main__":
    main()
