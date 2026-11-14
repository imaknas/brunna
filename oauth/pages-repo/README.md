# brunna-dev.github.io

This repository publishes one file: brunna's shared OAuth client document,
`https://brunna-dev.github.io/oauth/client.json`. Every brunna install presents itself to OAuth providers with it
(a Client ID Metadata Document; brunna's `docs/design/platform-credentials.md`, C2a).

Whoever can change this file can make any provider show a "brunna" consent screen with their own redirect, so it is
guarded:

- `oauth/client.json` is a copy of the same file at a brunna release tag. It changes only by a pull request,
  reviewed, at a brunna release. A change to any of its security fields (`client_id`, `redirect_uris`, `client_name`,
  `grant_types`, the auth method fields) is never an edit: it is a new file, `oauth/client-2.json`, with a new
  `client_id`, and the old one stays published.
- `main` is protected: pull requests only, no force-push, no deletion.
- Pages is built only by `.github/workflows/pages.yml`. Its actions are pinned by commit SHA, and it publishes exactly
  that one file, with no links. The `github-pages` environment accepts deployments from `main` only.
- The organization requires two-factor authentication of its members.
- Every brunna install compares this file with its own copy once a day (`brunna credentials check-published`) and
  alerts on a difference.

This folder is a template kept in brunna's repository (`oauth/pages-repo/`). The setup steps are in brunna's
`docs/design/platform-credentials.md`, "Order of work", step 4.
