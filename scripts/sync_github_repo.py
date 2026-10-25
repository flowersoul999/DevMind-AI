"""DevMind AI module."""

import argparse
import asyncio

from app.services.github.client import GitHubClient


async def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test GitHub repository access.")
    parser.add_argument("owner")
    parser.add_argument("repo")
    args = parser.parse_args()
    data = await GitHubClient().get_repo(args.owner, args.repo)
    print({"full_name": data.get("full_name"), "default_branch": data.get("default_branch")})


if __name__ == "__main__":
    asyncio.run(main())
