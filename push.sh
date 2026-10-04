#!/bin/sh
# Push ND36 to GitHub and GitLab.
#
#   sh push.sh
#
# Both remotes are already configured. The first push asks for a
# username and a token (NOT your account password -- GitHub and GitLab
# both stopped accepting passwords over https):
#
#   GitHub  Settings -> Developer settings -> Personal access tokens
#           -> Tokens (classic) -> Generate, tick "repo"
#   GitLab  Preferences -> Access tokens -> Add new token,
#           tick "write_repository"
#
# To be asked only once:  git config --global credential.helper osxkeychain
set -e
cd "$(dirname "$0")"

echo "→ github"
git push -u origin main

echo "→ gitlab"
git push -u gitlab main

echo
echo "done. both remotes are at $(git rev-parse --short HEAD)."
