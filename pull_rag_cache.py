#!/usr/bin/env python3
"""
Downloads the pre-built `repo_index` RAG database from GitLab CI to speed up local indexing.

Requires a GitLab Personal Access Token (PAT) with at least `read_api` scope set in the
`GITLAB_TOKEN` environment variable.
"""

import os
import json
import urllib.request
import zipfile
import shutil
import io
import sys
from pathlib import Path

def pull_cache() -> bool:
    token = os.environ.get("GITLAB_TOKEN")
    if not token:
        print("❌ Error: GITLAB_TOKEN environment variable is not set. A token is required to download artifacts.")
        return False

    here = Path(__file__).parent.resolve()
    config_file = here / "config.json"

    if not config_file.exists():
        print(f"❌ Error: Config file not found at {config_file}")
        return False

    with open(config_file, "r") as f:
        config = json.load(f)

    gitlab_cfg = config.get("gitlab", {})
    project_id = gitlab_cfg.get("project_id")
    host = gitlab_cfg.get("host", "https://gitlab.com")
    branch = gitlab_cfg.get("branch", "main")
    job_name = gitlab_cfg.get("rag_job_name", "build-rag-index")

    if not project_id or project_id == "YOUR_PROJECT_ID":
        print("❌ Error: Valid `project_id` must be set in .tools/config.json under the `gitlab` section.")
        return False

    # API Endpoint for downloading artifacts of a specific job on a specific branch
    api_url = f"{host}/api/v4/projects/{urllib.parse.quote(project_id, safe='')}/jobs/artifacts/{branch}/download?job={urllib.parse.quote(job_name, safe='')}"

    print(f"Fetching RAG index from {host} for project '{project_id}' out of job '{job_name}' on branch '{branch}'...")
    
    req = urllib.request.Request(api_url)
    req.add_header("PRIVATE-TOKEN", token)

    try:
        with urllib.request.urlopen(req) as response:
            if response.status == 200:
                print("Download complete. Extracting...")
                with zipfile.ZipFile(io.BytesIO(response.read())) as z:
                    repo_index_dir = here / "rag" / "repo_index"
                    if repo_index_dir.exists():
                        print("Cleaning up old index directory...")
                        shutil.rmtree(repo_index_dir)
                    
                    project_root = here.parent
                    z.extractall(project_root)
                print(f"✅ Successfully downloaded and extracted RAG database to: {repo_index_dir}")
                return True
            else:
                print(f"❌ Failed to download artifact. HTTP Status: {response.status}")
                return False
    except urllib.error.HTTPError as e:
        print(f"❌ HTTP Error: {e.code} - {e.reason}")
        if e.code == 404:
             print("   (Either the job doesn't exist, no artifacts were found, or the project ID is wrong)")
        return False
    except Exception as e:
        print(f"❌ An error occurred: {e}")
        return False

def main():
    success = pull_cache()
    if not success:
        sys.exit(1)

if __name__ == "__main__":
    main()
