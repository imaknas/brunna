## This project is an idea sandbox

The registry marks this project `type: idea`: it was opened to explore an idea. That means:

- It has no GitHub remote: its history lives on this machine and in the platform's encrypted backup only.
- Idea sandboxes are sandboxed by default (`enforce.sandbox` in the registry entry; `brunna status <project>` shows the
  entry): the project writes only its own folder, reads no other project and none of the credential folders, and
  reaches only the hosts its entry lists (its services and jobs can't reach the Keychain either). So `gh` and pushing to
  GitHub don't work from here.
- Upgrading it to a project gives it a GitHub repo (only its main branch is pushed; the memory branch never leaves this
  machine) and changes its type. The sandbox stays on. Only {{owner}} decides.

When it outgrows exploration (it keeps credentials of its own, declares tests, a service or a job, needs a provider
billed per use or hosts beyond the package indexes, or is going to be published), ask for the upgrade rather than
working around the sandbox:

    brunna send --to brunna --type request --text "upgrade <project> to a project: why; public or private"

The platform also lists it for {{owner}} every night once its registry entry shows those signals.
